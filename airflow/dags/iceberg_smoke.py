"""iceberg_smoke — first real Iceberg DAG against Lakekeeper.

Validates the data path end-to-end:
  Airflow -> PyIceberg (FsspecFileIO) -> Lakekeeper REST -> MinIO (s3://iceberg-warehouse/)

History (sessions 9 + 10):
  - DuckDB-iceberg path failed: even with an explicit `CREATE SECRET (TYPE s3)`,
    DuckDB's iceberg writer uses the catalog-vended storage-credentials in
    preference to user secrets. With Lakekeeper's default warehouse settings,
    those vended credentials are EMPTY (Lakekeeper expects remote-signing OR
    STS, neither of which DuckDB implements for MinIO).
  - PyIceberg + the default PyArrow FileIO failed: PyArrow's bundled S3 client
    doesn't honor `AWS_CA_BUNDLE` (curl SSL error 60 against MinIO's
    Kubernetes-CSR-signed cert).
  - The Lakekeeper warehouse storage profile was patched out-of-band to
    `remote-signing-enabled: false` (POST /management/v1/warehouse/{id}/storage),
    which stops the catalog from advertising the signer endpoint to clients.
  - FsspecFileIO + static creds + the env-injected cluster CA bundle works
    cleanly. That is what this DAG uses.

Warehouse 'demo' was created via /tmp/setup-lakekeeper-secrets.sh, the airflow
MinIO svcacct is scoped to bucket iceberg-warehouse, and the keys are reflected
into the airflow namespace as LAKE_S3_ACCESS_KEY_ID / LAKE_S3_SECRET_ACCESS_KEY.

Schedule: manual trigger only.
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task


LAKEKEEPER_URL = "http://lakekeeper.lakekeeper.svc.cluster.local:8181"
WAREHOUSE = "demo"
NAMESPACE = "test"
TABLE = "iceberg_smoke"


def _build_catalog():
    """Build a RestCatalog handle that bypasses Lakekeeper's empty
    vended-credentials by passing our own static svcacct keys in. Uses
    FsspecFileIO so that the standard s3fs/aiobotocore stack honors
    AWS_CA_BUNDLE for MinIO's cluster-CA-signed cert."""
    import os
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog(
        name=WAREHOUSE,
        uri=f"{LAKEKEEPER_URL}/catalog",
        warehouse=WAREHOUSE,
        **{
            "py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
            "s3.endpoint": "https://minio.data-platform.svc.cluster.local/",
            "s3.access-key-id": os.environ["LAKE_S3_ACCESS_KEY_ID"],
            "s3.secret-access-key": os.environ["LAKE_S3_SECRET_ACCESS_KEY"],
            "s3.region": "us-east-1",
            "s3.path-style-access": "true",
        },
    )


@dag(
    dag_id="iceberg_smoke",
    start_date=datetime(2026, 5, 22),
    schedule=None,
    catchup=False,
    tags=["validation", "iceberg"],
    doc_md=__doc__,
)
def iceberg_smoke():
    @task
    def ensure_namespace() -> str:
        catalog = _build_catalog()
        try:
            catalog.create_namespace(NAMESPACE)
            print(f"Created namespace {NAMESPACE!r}")
        except Exception as e:
            print(f"Namespace {NAMESPACE!r} already exists: {e}")
        return NAMESPACE

    @task
    def write_via_pyiceberg(namespace: str) -> int:
        """Drop + recreate the table, append 5 rows, return row count."""
        import pyarrow as pa

        catalog = _build_catalog()
        ident = (namespace, TABLE)

        try:
            catalog.drop_table(ident)
            print(f"Dropped existing table {ident}")
        except Exception:
            pass

        rows = pa.table(
            {
                "id": [1, 2, 3, 4, 5],
                "name": ["alice", "bob", "carol", "dave", "eve"],
                "value": [100, 200, 300, 400, 500],
            }
        )
        tbl = catalog.create_table(ident, schema=rows.schema)
        tbl.append(rows)
        n = len(tbl.scan().to_arrow())
        print(f"Wrote {n} rows to {namespace}.{TABLE}")
        return n

    @task
    def read_back_via_pyiceberg(namespace: str, expected_rows: int) -> None:
        """Fresh catalog handle proves rows persisted to MinIO, not just
        in-process state."""
        catalog = _build_catalog()
        tbl = catalog.load_table((namespace, TABLE))
        result = tbl.scan().to_arrow().to_pylist()
        print(f"Read {len(result)} rows:")
        for r in result:
            print(f"  {r}")
        assert len(result) == expected_rows, (
            f"Row count mismatch -- wrote {expected_rows}, read {len(result)}"
        )

    ns = ensure_namespace()
    n = write_via_pyiceberg(ns)
    read_back_via_pyiceberg(ns, n)


iceberg_smoke()
