"""insurance_retention_promote -- the gated promotion step, as an auditable DAG that emits the
production Asset so the scoring DAG can run on it.

promote_bundle.py (training/) is a standalone script: it atomically sets the @production alias on
the three component models + the meta bundle, gated on an NV-uplift floor, with rollback if any
alias set fails. Wrapping it in a DAG (rather than running it as a raw Job) buys two things:
  1. an Airflow audit trail of who promoted what, when, with which floor; and
  2. an Asset OUTLET -- "a promotion happened" becomes a first-class lineage node that the scoring
     DAG schedules on. A script cannot emit an Airflow Asset; a DAG task can.

schedule=None: promotion stays DELIBERATELY manual + gated (trigger from the UI or `airflow dags
trigger`, optionally with conf {bundle_id, nv_uplift_floor, image}). The KPO enforces the NV floor +
rollback; the downstream announce task -- which emits insurance_retention_production -- runs ONLY if
that KPO SUCCEEDS, so a failed or refused promotion never triggers downstream scoring.

Pod wiring: MLflow tracking + MinIO creds only (alias ops touch the registry, not the lakehouse).
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task, Variable, Asset, get_current_context
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"

# Produced Asset -- name+uri MUST match the consumer (insurance_retention_scoring.py).
PRODUCTION_ASSET = Asset(name="insurance_retention_production", uri="mlflow://insurance-retention-bundle@production")


@dag(
    dag_id="insurance_retention_promote",
    start_date=datetime(2026, 5, 28),
    schedule=None,  # deliberately manual + gated -- a human consciously promotes
    catchup=False,
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "promotion"],
    doc_md=__doc__,
)
def insurance_retention_promote():
    @task
    def resolve_image() -> str:
        """Image to run: conf override, else the sensor's target, else :latest. (promote_bundle.py
        only touches the registry, so any recent image works; reuse the resolved target.)"""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return conf.get("image") or Variable.get("ir_target_image", default=IMAGE)

    image = resolve_image()

    promote = KubernetesPodOperator(
        task_id="promote_bundle",
        name="insurance-retention-promote",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        # Module form (matches train/score): puts the image WORKDIR (/app) on sys.path so any
        # repo-root import added to promote_bundle.py later resolves -- script form would break it
        # the way it broke train.py. promote_bundle has no repo-root imports today; this is the
        # safe-form-only invariant, enforced by the CI image smoke test.
        cmds=["python", "-m", "training.promote_bundle"],
        arguments=[
            "--bundle-id", "{{ (dag_run.conf or {}).get('bundle_id', 'latest') }}",
            "--nv-uplift-floor", "{{ (dag_run.conf or {}).get('nv_uplift_floor', 0.3) }}",
            "--max-regression", "{{ (dag_run.conf or {}).get('max_regression', 0.05) }}",
        ],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
        },
        secrets=[
            Secret("env", "AWS_ACCESS_KEY_ID", "insurance-retention-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "AWS_SECRET_ACCESS_KEY", "insurance-retention-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
        ],
        volumes=[
            k8s.V1Volume(name="cluster-ca", config_map=k8s.V1ConfigMapVolumeSource(name="kube-root-ca.crt")),
        ],
        volume_mounts=[
            k8s.V1VolumeMount(name="cluster-ca", mount_path="/etc/ssl/k3s/ca.crt", sub_path="ca.crt", read_only=True),
        ],
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        security_context=k8s.V1PodSecurityContext(
            run_as_non_root=True, run_as_user=1001, run_as_group=1001, fs_group=1001,
            seccomp_profile=k8s.V1SeccompProfile(type="RuntimeDefault"),
        ),
        container_security_context=k8s.V1SecurityContext(
            allow_privilege_escalation=False, run_as_non_root=True,
            read_only_root_filesystem=False, capabilities=k8s.V1Capabilities(drop=["ALL"]),
        ),
        get_logs=True,
        on_finish_action="delete_pod",
        startup_timeout_seconds=300,
    )

    @task(outlets=[PRODUCTION_ASSET])
    def announce_promotion() -> None:
        """Emit the production Asset. Runs ONLY after the promote KPO SUCCEEDS (gate passed, all four
        aliases set), so a failed or NV-floor-refused promotion never triggers downstream scoring."""
        print("Promotion succeeded; emitting insurance_retention_production to trigger scoring.")

    image >> promote >> announce_promotion()


insurance_retention_promote()
