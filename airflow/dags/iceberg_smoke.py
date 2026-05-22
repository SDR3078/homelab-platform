"""iceberg_smoke — first real Iceberg DAG against Lakekeeper.

Validates the data path end-to-end:
  Airflow → DuckDB → Lakekeeper REST Catalog → MinIO (s3://iceberg-warehouse/)

Approach: DuckDB's `iceberg` extension supports the Iceberg REST Catalog
spec since 1.4. Attaches Lakekeeper as a logical catalog, creates the
namespace + table, inserts rows, reads them back. Bypasses the
pyiceberg FsspecFileIO + S3V4RestSigner gap discovered in session 9 —
DuckDB has its own native S3 client + iceberg writer.

CAVEAT: DuckDB + Lakekeeper's REST + remote-signing integration is
relatively new. If DuckDB's S3 client doesn't handle Lakekeeper's
remote-signing protocol (the same gap pyiceberg's fsspec backend has),
the write step will fail with AccessDenied — in which case we surface
which step broke and iterate (likely by configuring DuckDB to use
Lakekeeper's vended credentials, or by managing the warehouse to vend
static credentials instead of remote-signing).

Warehouse 'demo' was created via /tmp/lakekeeper-validate.sh in
session 9 with the airflow MinIO svcacct scoped to iceberg-warehouse.
The svcacct creds are stored encrypted in Lakekeeper's Postgres backend
under the catalog's encryption key.

Schedule: manual trigger only.
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task


LAKEKEEPER_URL = "http://lakekeeper.lakekeeper.svc.cluster.local:8181"
WAREHOUSE = "demo"
NAMESPACE = "test"
TABLE = "iceberg_smoke"


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
        """Create the Iceberg namespace via PyIceberg's RestCatalog (catalog
        ops are the proven-working path — read confirmed in session 9)."""
        from pyiceberg.catalog.rest import RestCatalog

        catalog = RestCatalog(
            name=WAREHOUSE,
            uri=f"{LAKEKEEPER_URL}/catalog",
            warehouse=WAREHOUSE,
        )
        try:
            catalog.create_namespace(NAMESPACE)
            print(f"Created namespace {NAMESPACE!r}")
        except Exception as e:
            # AlreadyExistsException is fine — idempotent.
            print(f"Namespace {NAMESPACE!r} already exists: {e}")
        return NAMESPACE

    @task
    def write_via_duckdb(namespace: str) -> int:
        """Attach Lakekeeper as a DuckDB iceberg catalog, create table +
        insert rows. Returns the number of rows inserted."""
        import duckdb

        con = duckdb.connect()
        con.execute("INSTALL iceberg; LOAD iceberg;")
        con.execute("INSTALL httpfs; LOAD httpfs;")

        # Configure DuckDB's REST catalog secret pointing at Lakekeeper.
        # No bearer token (Lakekeeper is in allowall auth mode for now).
        # TOKEN '' forces DuckDB to skip its default OAuth2 auth path
        # (which requires CLIENT_ID + CLIENT_SECRET). Lakekeeper is in
        # `allowall` auth mode for the homelab — no auth required.
        con.execute(
            f"""
            CREATE OR REPLACE SECRET lk_iceberg (
                TYPE iceberg,
                ENDPOINT '{LAKEKEEPER_URL}/catalog',
                TOKEN ''
            );
            """
        )
        con.execute(
            f"ATTACH '{WAREHOUSE}' AS lake (TYPE iceberg, SECRET lk_iceberg);"
        )

        # Create or replace the test table + insert 5 rows.
        # CREATE OR REPLACE is idempotent across DAG runs.
        con.execute(
            f"""
            CREATE OR REPLACE TABLE lake.{namespace}.{TABLE} AS
            SELECT * FROM (VALUES
                (1, 'alice',   100),
                (2, 'bob',     200),
                (3, 'carol',   300),
                (4, 'dave',    400),
                (5, 'eve',     500)
            ) AS tbl(id, name, value);
            """
        )

        n = con.execute(f"SELECT COUNT(*) FROM lake.{namespace}.{TABLE}").fetchone()[0]
        print(f"Wrote {n} rows to lake.{namespace}.{TABLE}")
        return n

    @task
    def read_back_via_duckdb(namespace: str, expected_rows: int) -> None:
        """Re-attach the catalog from a fresh DuckDB connection (proves the
        rows survive in MinIO, not just in this process's memory). Logs the
        rows + asserts count matches what we wrote."""
        import duckdb

        con = duckdb.connect()
        con.execute("INSTALL iceberg; LOAD iceberg;")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        # TOKEN '' forces DuckDB to skip its default OAuth2 auth path
        # (which requires CLIENT_ID + CLIENT_SECRET). Lakekeeper is in
        # `allowall` auth mode for the homelab — no auth required.
        con.execute(
            f"""
            CREATE OR REPLACE SECRET lk_iceberg (
                TYPE iceberg,
                ENDPOINT '{LAKEKEEPER_URL}/catalog',
                TOKEN ''
            );
            """
        )
        con.execute(
            f"ATTACH '{WAREHOUSE}' AS lake (TYPE iceberg, SECRET lk_iceberg);"
        )

        rows = con.execute(
            f"SELECT id, name, value FROM lake.{namespace}.{TABLE} ORDER BY id"
        ).fetchall()
        print(f"Read {len(rows)} rows:")
        for r in rows:
            print(f"  {r}")

        assert len(rows) == expected_rows, (
            f"Row count mismatch — wrote {expected_rows}, read {len(rows)}"
        )

    ns = ensure_namespace()
    n = write_via_duckdb(ns)
    read_back_via_duckdb(ns, n)


iceberg_smoke()
