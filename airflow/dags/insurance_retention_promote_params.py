"""insurance_retention_promote_params -- activate a tuned-params version for the CT.

The deliberate, gated step in the tuning lifecycle: a KubernetesPodOperator runs
`promote_params.py`, which sets the @production alias on an `insurance-retention-params` version. The
train DAG's init container resolves that alias into BEST_PARAMS_PATH, so the NEXT train run refits
these params. Mirrors insurance_retention_promote (the bundle promotion), MINUS an NV gate -- params
have no metric until a bundle trains on them, so this is a human decision and the bundle NV
non-regression gate validates the RESULT downstream (best_params_sha traces which params built it).

schedule=None: deliberately manual (trigger from the UI / `airflow dags trigger`, optionally with conf
{version}). promote_params refuses to ship a non-real params version by default, so this activates the
cluster's real-data tune, not the synthetic demo default. It does NOT itself trigger a retrain -- run
insurance_retention_train afterwards (or on the next image/bronze edge) to refit the new params.

Pod wiring: MLflow tracking + MinIO creds only (alias ops touch the registry, not the lakehouse).
"""
from __future__ import annotations

from datetime import datetime

from airflow.sdk import Variable, dag, get_current_context, task
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"


@dag(
    dag_id="insurance_retention_promote_params",
    start_date=datetime(2026, 6, 1),
    schedule=None,  # deliberately manual -- a human activates which tuned params the CT refits
    catchup=False,
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "tuning"],
    doc_md=__doc__,
)
def insurance_retention_promote_params():
    @task
    def resolve_image() -> str:
        """Image to run: conf override, else the sensor's target, else :latest (registry ops only)."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return conf.get("image") or Variable.get("ir_target_image", default=IMAGE)

    image = resolve_image()

    promote = KubernetesPodOperator(
        task_id="promote_params",
        name="insurance-retention-promote-params",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        cmds=["python", "-m", "training.promote_params"],
        # Default 'latest' = the most recently registered version (the tune DAG's run). promote_params
        # refuses a non-real version unless --allow-synthetic, which the cluster path deliberately omits.
        arguments=["--version", "{{ (dag_run.conf or {}).get('version', 'latest') }}"],
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

    image >> promote


insurance_retention_promote_params()
