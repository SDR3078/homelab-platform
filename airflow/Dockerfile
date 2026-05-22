# Custom Airflow task image — homelab-platform
#
# FROM apache/airflow:3.2.0 + DataFrame/Iceberg/DuckDB stack for ML/data
# engineering tasks. Used by:
#   - Airflow scheduler, api-server, triggerer, dag-processor (all share
#     the same image since chart values bump defaultAirflowRepository +
#     defaultAirflowTag globally)
#   - KubernetesExecutor task pods (each task = fresh pod with this image)
#
# Tagged with git SHA. CI builds + pushes on changes to:
#   - airflow/Dockerfile
#   - airflow/requirements.txt
# DAG-only changes (airflow/dags/**) do NOT rebuild the image — git-sync
# sidecar picks them up directly.
#
# Image registry: ghcr.io/sdr3078/homelab-platform-airflow:<sha>
# Public visibility (no pull secret needed).

FROM apache/airflow:3.2.0

# Install Python deps as the airflow user (not root) — keeps PSA-restricted
# compliance on task pods. apache/airflow base image's airflow user has
# write access to /home/airflow/.local for --user installs.
USER airflow

COPY --chown=airflow:0 requirements.txt /requirements.txt

RUN pip install --no-cache-dir --user --requirement /requirements.txt && \
    pip cache purge

# Image labels for traceability — GHCR's package UI surfaces these.
LABEL org.opencontainers.image.source="https://github.com/SDR3078/homelab-platform"
LABEL org.opencontainers.image.description="Airflow task image with PyIceberg + DuckDB + pandas for ML/data workflows"
LABEL org.opencontainers.image.licenses="Apache-2.0"
