"""hello_world — first DAG in the homelab platform.

Validates the end-to-end Airflow pipeline:
1. git-sync sidecar picks up DAG file changes (this repo's airflow/dags/)
2. Scheduler parses the DAG without errors
3. KubernetesExecutor spawns a task pod using the custom image
4. Task pod runs Python with pyiceberg + duckdb + pandas importable
5. Logs surface in the Airflow UI (via remote logging to s3://airflow-logs/)

Validated end-to-end on 2026-05-22.

Schedule: manual trigger only.
"""

from __future__ import annotations

import sys
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="hello_world",
    start_date=datetime(2026, 5, 22),
    schedule=None,  # manual trigger only
    catchup=False,
    tags=["validation", "scaffolding"],
    doc_md=__doc__,
)
def hello_world():
    @task
    def print_environment() -> dict:
        """Confirm the custom image's deps are importable + log Python env."""
        import duckdb
        import pandas as pd
        import pyarrow as pa
        import pyiceberg

        info = {
            "python_version": sys.version,
            "pyiceberg_version": pyiceberg.__version__,
            "duckdb_version": duckdb.__version__,
            "pandas_version": pd.__version__,
            "pyarrow_version": pa.__version__,
        }
        for k, v in info.items():
            print(f"{k}: {v}")
        return info

    @task
    def smoke_duckdb() -> int:
        """Run a trivial DuckDB query — proves DuckDB works in-pod."""
        import duckdb

        result = duckdb.sql("SELECT 42 AS answer").fetchone()
        print(f"DuckDB SELECT 42 → {result}")
        return result[0]

    print_environment() >> smoke_duckdb()


hello_world()
