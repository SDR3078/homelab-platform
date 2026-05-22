# airflow/ — task image + DAGs for the homelab platform

Contains everything that runs INSIDE Airflow task pods:

- `Dockerfile` — custom Airflow image based on `apache/airflow:3.2.0` +
  PyIceberg + DuckDB + pandas + pyarrow. Required because Airflow's
  KubernetesExecutor task pods need these libs to read/write Iceberg
  tables via Lakekeeper without going through the broken pyiceberg
  FsspecFileIO + S3V4RestSigner path (see
  `notes/session-2026-05-22.md` for the full story).
- `requirements.txt` — top-level Python deps, range-pinned per
  major.minor. Single-file v1 — future enhancement is the two-file
  pattern with `requirements.lock` generated via `pip-compile
  --generate-hashes` for full transitive + hash pinning.
- `dags/` — Airflow DAG Python files, git-synced into the scheduler
  via `apps/airflow.yaml`'s `dags.gitSync` config.
- `dags/.airflowignore` — patterns Airflow's parser should skip.

## How CI handles changes here

`.github/workflows/airflow-image.yaml` (in repo root, NOT in this
directory) is path-filtered to fire only on changes to:
- `airflow/Dockerfile`
- `airflow/requirements.txt`

On those changes, CI builds the image, pushes it to
`ghcr.io/sdr3078/homelab-platform-airflow:<sha>`, then auto-commits a
bump to `apps/airflow.yaml`'s `defaultAirflowTag` (with `[skip ci]` to
avoid loops). ArgoCD detects the manifest change and rolls Airflow.

Changes to `airflow/dags/**` do NOT trigger the image build —
`.github/workflows/airflow-dag-parse.yaml` runs a parse-smoke-test
(import-only) on DAG-only changes, then git-sync sidecar picks the
new DAG up within seconds.

## Adding a new DAG

1. Create `airflow/dags/your_dag.py`
2. Commit + push
3. CI runs DAG-parse smoke test (catches `ImportError` etc.)
4. Once merged to main, git-sync (inside the Airflow scheduler pod)
   pulls the new file within ~30s
5. DAG appears in the Airflow UI

## Adding a new Python dep

1. Edit `airflow/requirements.txt`
2. Commit + push
3. CI rebuilds the image + auto-bumps `apps/airflow.yaml`
4. ArgoCD rolls Airflow with the new image

## Bumping the Airflow base image

1. Edit `airflow/Dockerfile`'s `FROM apache/airflow:<version>`
2. Also bump `apps/airflow.yaml`'s `airflowVersion` and chart targetRevision
   if there's a matching chart upgrade
3. Commit + push — same flow as a Python-dep bump

## Local DAG dev (optional)

For DAG development before pushing:
```bash
# Local virtualenv with the same deps
python -m venv /tmp/airflow-dev
source /tmp/airflow-dev/bin/activate
pip install --requirement airflow/requirements.txt apache-airflow==3.2.0

# Smoke-parse a single DAG
python airflow/dags/your_dag.py

# Full Airflow standalone (heavier — use the K8s deployment for serious dev)
airflow standalone
```
