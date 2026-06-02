"""insurance_retention_bronze_score_ingest -- land the 2022 scoring population in the lakehouse.

Mirrors insurance_retention_bronze_ingest (the training-data ingest) for the SCORING input:
score.csv lands as parquet in a MinIO landing zone, and this DAG writes it to Iceberg
insurance_retention.bronze.score via Lakekeeper. The batch scorer (pipelines/score.py, in the model
image) later reads bronze.score at a pinned snapshot and runs build_features IN-PROCESS -- so
bronze.score is raw + load-bearing, exactly like bronze.train; there is no silver/feature table.

Why a SEPARATE DAG (not a task in the scoring DAG -- data-engineer + mlops-engineer review,
2026-05-28): score data changes on the order of once per season, while the dominant re-score
trigger is a model promotion. A standalone ingest keeps the two lifecycles independent and legible
in the Asset graph -- a new score population fires the `insurance_retention_bronze_score` Asset (one
trigger of the scoring DAG); a promotion fires the production Asset (the other). The content-hash
watermark skips no-op re-ingests, mirroring bronze.train + the image-sensor.

Design notes:
- candidate_id = row number (0-indexed) over the landed file's PHYSICAL order, stamped HERE and
  nowhere else. score.csv has no natural id; this is the stable key that lets the batch selection
  line up with the live serving /select (whose candidate_id is the POST-order position, also
  0-indexed). INVARIANT: never re-order the landing file -- the row order IS the id contract.
- INFER the schema (no hand schema): score uses DOB (not Birth_date) and has NO targets.
- Hygiene: ONLY parity-neutral fixes (strip whitespace column names + drop an Unnamed index),
  exactly as bronze.train -- build_features re-does both. NO renames (the wheel owns those, and it
  does not rename, so a bronze rename would diverge score-from-bronze from serve-from-API).
- DQ gate for a no-targets scoring input: exactly 500 rows; targets ABSENT (a present target means
  a file swap); DOB present and Birth_date absent (the one schema divergence build_features handles).
- Provenance: _ingested_at, _source_uri, _content_sha256.

Prereq: score.csv must be landed once at LANDING_URI as parquet (independent of any image), the
score-side analogue of the df_final landing seed.

Runs in the Airflow worker (pyiceberg + s3fs); reuses the iceberg_smoke recipe (RestCatalog +
FsspecFileIO + static LAKE creds + path-style; AWS_CA_BUNDLE trusts MinIO's cluster cert). Manual
trigger; active on creation.
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
TABLE = "score"
LANDING_URI = "s3://iceberg-warehouse/landing/insurance_retention/score.parquet"
EXPECTED_ROWS = 500
WATERMARK_VAR = "ir_score_content_sha"
SNAPSHOT_VAR = "ir_score_snapshot_id"  # exposes the landed snapshot id to the scoring DAG
CANDIDATE_ID = "candidate_id"
TARGET_COLS = ("Profit_insurance", "Covid", "Covid_amt")

BRONZE_SCORE_ASSET = Asset(
    name="insurance_retention_bronze_score",
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
    dag_id="insurance_retention_bronze_score_ingest",
    start_date=datetime(2026, 5, 28),
    schedule=None,
    catchup=False,
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "data", "iceberg", "scoring"],
    doc_md=__doc__,
)
def insurance_retention_bronze_score_ingest():
    @task(outlets=[BRONZE_SCORE_ASSET])
    def load_bronze_score() -> None:
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
            raise AirflowSkipException(f"bronze.{TABLE} unchanged (sha {content_sha[:12]}); skipping")

        df = pd.read_parquet(io.BytesIO(raw))

        # 2. candidate_id from the file's PHYSICAL row order, BEFORE any hygiene/reorder. 0-indexed
        #    to match serving /select's POST-position id. Assigned HERE and nowhere else.
        df.insert(0, CANDIDATE_ID, range(len(df)))

        # 3. Hygiene -- parity-neutral only (build_features re-does both). NO renames.
        df.columns = [c.strip() for c in df.columns]
        df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")], errors="ignore")

        # 4. DQ gate for a no-targets scoring input.
        assert len(df) == EXPECTED_ROWS, f"row count {len(df)} != {EXPECTED_ROWS}"
        present_targets = [c for c in TARGET_COLS if c in df.columns]
        assert not present_targets, f"scoring input must carry NO targets; found {present_targets} (file swap?)"
        assert "DOB" in df.columns, "scoring input must have DOB"
        assert "Birth_date" not in df.columns, "scoring input uses DOB, not Birth_date (mis-exported file?)"

        # 5. Provenance.
        df["_ingested_at"] = pd.Timestamp.now(tz=timezone.utc)
        df["_source_uri"] = LANDING_URI
        df["_content_sha256"] = content_sha

        # Iceberg timestamps are microsecond precision; pandas datetimes are ns, which PyIceberg
        # rejects on write. Downcast ns->us (mirrors bronze.train).
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.as_unit("us")

        arrow = pa.Table.from_pandas(df, preserve_index=False)

        # 6. Create namespaces if needed; OVERWRITE (preserves table identity + snapshot history,
        #    which the gold lineage's score_snapshot_id points at).
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
        # Expose the landed snapshot id for the scoring DAG to PIN (the data leg of the scoring
        # lineage): score.py reads bronze.score@snap and stamps it into gold as score_snapshot_id.
        Variable.set(SNAPSHOT_VAR, str(snap))
        n = len(t.scan().to_arrow())
        print(f"bronze.{TABLE}: {n} rows, snapshot_id={snap}, content_sha={content_sha[:12]}")

    load_bronze_score()


insurance_retention_bronze_score_ingest()
