"""insurance_retention_bronze_ingest -- land the raw training data in the lakehouse.

De-bakes the training data from the runtime image: the raw df_final lands in a
MinIO landing zone (seeded once, independent of any image), and this DAG writes it
to Iceberg `insurance_retention.bronze.train` via Lakekeeper. Training later reads
bronze (raw) at a PINNED snapshot and runs `build_features` IN-PROCESS, preserving
train/serve parity through the wheel -- so bronze is the load-bearing layer;
silver/gold tables are optional analytics artifacts, NOT the model's feature source
(a materialized feature table would re-introduce the skew the wheel kills: pandas
category dtypes + fitted medians don't round-trip cleanly through Iceberg).

Design (from the data-engineer + mlops-engineer review):
- OVERWRITE, never drop+recreate -- preserves the table identity + snapshot log that
  the bundle's `data_snapshot_id` lineage will point at.
- Fire the `insurance_retention_bronze` Asset ONCE PER CONTENT CHANGE: a content-hash
  watermark (Airflow Variable `ir_bronze_content_sha`) skips the run when the landing
  bytes are unchanged, so a static dataset never loops spurious retrains (mirrors the
  image-sensor's digest watermark).
- Bronze hygiene: strip the case's trailing-whitespace column names, rename
  `non-antibiotics`, drop the `Unnamed: 0` Excel index; keep targets nullable as
  landed (NO Covid-label recovery here -- that's a silver/`build_features` decision).
- A minimal structural DQ gate (row count + targets present + Covid=0 => Covid_amt=0)
  before the write; no heavy DQ framework.
- Provenance columns: `_ingested_at`, `_source_uri`, `_content_sha256`.

Runs in the Airflow worker (which has pyiceberg + s3fs); reuses the proven
iceberg_smoke recipe (RestCatalog + FsspecFileIO + static LAKE creds + path-style;
AWS_CA_BUNDLE trusts MinIO's cluster cert). Manual trigger; active on creation.
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task, Variable, Asset
from airflow.sdk.exceptions import AirflowSkipException

LAKEKEEPER_URL = "http://lakekeeper.lakekeeper.svc.cluster.local:8181"
MINIO_ENDPOINT = "https://minio.data-platform.svc.cluster.local"
WAREHOUSE = "demo"
NS_PARENT = ("insurance_retention",)
NS = ("insurance_retention", "bronze")
TABLE = "train"
LANDING_URI = "s3://iceberg-warehouse/landing/insurance_retention/df_final.parquet"
EXPECTED_ROWS = 5000
WATERMARK_VAR = "ir_bronze_content_sha"

BRONZE_ASSET = Asset(
    name="insurance_retention_bronze",
    uri=f"iceberg://{WAREHOUSE}/insurance_retention.bronze.{TABLE}",
)


def _s3_storage_options() -> dict:
    import os

    return {
        "key": os.environ["LAKE_S3_ACCESS_KEY_ID"],
        "secret": os.environ["LAKE_S3_SECRET_ACCESS_KEY"],
        "client_kwargs": {"endpoint_url": MINIO_ENDPOINT, "region_name": "us-east-1"},
        "config_kwargs": {"s3": {"addressing_style": "path"}},
    }


def _build_catalog():
    """Proven iceberg_smoke recipe: RestCatalog + FsspecFileIO + static LAKE creds."""
    import os
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog(
        name=WAREHOUSE,
        uri=f"{LAKEKEEPER_URL}/catalog",
        warehouse=WAREHOUSE,
        **{
            "py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
            "s3.endpoint": f"{MINIO_ENDPOINT}/",
            "s3.access-key-id": os.environ["LAKE_S3_ACCESS_KEY_ID"],
            "s3.secret-access-key": os.environ["LAKE_S3_SECRET_ACCESS_KEY"],
            "s3.region": "us-east-1",
            "s3.path-style-access": "true",
        },
    )


@dag(
    dag_id="insurance_retention_bronze_ingest",
    start_date=datetime(2026, 5, 25),
    schedule=None,
    catchup=False,
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "data", "iceberg"],
    doc_md=__doc__,
)
def insurance_retention_bronze_ingest():
    @task(outlets=[BRONZE_ASSET])
    def load_bronze() -> None:
        import hashlib
        import io
        from datetime import timezone

        import fsspec
        import pandas as pd
        import pyarrow as pa

        so = _s3_storage_options()

        # 1. Read the landing bytes once; hash them for the change watermark.
        fs = fsspec.filesystem(
            "s3", key=so["key"], secret=so["secret"],
            client_kwargs=so["client_kwargs"], config_kwargs=so["config_kwargs"],
        )
        with fs.open(LANDING_URI[len("s3://"):], "rb") as f:
            raw = f.read()
        content_sha = hashlib.sha256(raw).hexdigest()

        catalog = _build_catalog()
        ident = (*NS, TABLE)
        exists = catalog.table_exists(ident)
        if exists and content_sha == Variable.get(WATERMARK_VAR, default=""):
            raise AirflowSkipException(
                f"bronze.{TABLE} unchanged (sha {content_sha[:12]}); skipping"
            )

        # 2. Hygiene: whitespace col names, rename non-antibiotics, drop the Excel index.
        df = pd.read_parquet(io.BytesIO(raw))
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={"non-antibiotics": "non_antibiotics"})
        df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")], errors="ignore")

        # 3. Structural DQ gate (fixed-dataset contract).
        assert len(df) == EXPECTED_ROWS, f"row count {len(df)} != {EXPECTED_ROWS}"
        for col in ("Profit_insurance", "Covid", "Covid_amt"):
            assert col in df.columns, f"missing target column {col!r}"
        bad = df[(df["Covid"] == 0) & (df["Covid_amt"].fillna(0) > 0)]
        assert bad.empty, f"invariant Covid=0 => Covid_amt=0 violated in {len(bad)} rows"

        # 4. Provenance.
        df["_ingested_at"] = pd.Timestamp.now(tz=timezone.utc)
        df["_source_uri"] = LANDING_URI
        df["_content_sha256"] = content_sha

        arrow = pa.Table.from_pandas(df, preserve_index=False)

        # 5. Create namespaces (parent + child) if needed; OVERWRITE the data
        #    (preserves table identity + snapshot history for the lineage stamp).
        for ns in (NS_PARENT, NS):
            try:
                catalog.create_namespace(ns)
            except Exception:
                pass
        if not exists:
            catalog.create_table(ident, schema=arrow.schema)
        t = catalog.load_table(ident)
        t.overwrite(arrow)

        Variable.set(WATERMARK_VAR, content_sha)
        snap = t.current_snapshot().snapshot_id
        n = len(t.scan().to_arrow())
        print(f"bronze.{TABLE}: {n} rows, snapshot_id={snap}, content_sha={content_sha[:12]}")

    load_bronze()


insurance_retention_bronze_ingest()
