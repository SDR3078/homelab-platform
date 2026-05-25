"""insurance_retention_train -- train + register the model bundle on the cluster MLflow.

Orchestration only: this DAG does NOT run the ML code in an Airflow worker (the
Airflow image has no lightgbm / mlflow / features-wheel). Instead a
KubernetesPodOperator launches the purpose-built train/serve image
(ghcr.io/sdr3078/insurance-retention) which runs `train.py --register` -- the
same pod spec the earlier one-off verification Job proved.

It REGISTERS a new bundle version on the cluster MLflow (Postgres metadata +
MinIO artifacts); it does NOT promote it. Promotion stays a separate, gated step
(promote_bundle.py), so a retrain never auto-ships to production -- the clean
CT-vs-promotion split.

Triggers: manual (schedule=None here), or automatically by the
insurance_retention_image_sensor DAG, which fires this one with an immutable
image digest in dag_run.conf['image'] whenever CI publishes a new build
(code-driven CT). A data-driven trigger follows once the Iceberg bronze ingest
lands (Workstream D). Promotion stays the separate gated step regardless.

Pod wiring (mirrors the proven Job):
  - MLFLOW_TRACKING_URI -> in-cluster MLflow Service
  - MinIO creds via the sealed 'insurance-retention-s3-credentials' (this ns) +
    MLFLOW_S3_ENDPOINT_URL + AWS_CA_BUNDLE (cluster CA from kube-root-ca.crt)
  - PSA 'restricted'-compliant securityContext (non-root uid 1001, drop ALL, seccomp)
The KPO pod runs in the 'airflow' namespace, where the chart's
airflow-pod-launcher-role already grants pod-create RBAC.
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

# Default image for manual / ad-hoc runs. The code-driven CT trigger
# (insurance_retention_image_sensor) overrides this with an immutable digest via
# dag_run.conf['image'], so an automated retrain runs the exact build CI produced
# and the candidate bundle is stamped with that ref (lineage in train.py).
IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"
_IMAGE = "{{ dag_run.conf.get('image', '" + IMAGE + "') }}"


@dag(
    dag_id="insurance_retention_train",
    start_date=datetime(2026, 5, 25),
    schedule=None,
    catchup=False,
    # Active on creation: the sensor triggers this DAG, and a paused DAG's
    # triggered runs sit queued forever instead of executing.
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "training"],
    doc_md=__doc__,
)
def insurance_retention_train():
    KubernetesPodOperator(
        task_id="train_and_register",
        name="insurance-retention-train",
        namespace="airflow",
        image=_IMAGE,
        image_pull_policy="Always",
        cmds=["python", "/app/training/train.py"],
        arguments=["--register", "--experiment-name", "insurance-retention"],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
            "IR_IMAGE_REF": _IMAGE,  # lineage: the exact image this run executed
        },
        secrets=[
            Secret("env", "AWS_ACCESS_KEY_ID", "insurance-retention-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "AWS_SECRET_ACCESS_KEY", "insurance-retention-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
        ],
        volumes=[
            k8s.V1Volume(
                name="cluster-ca",
                config_map=k8s.V1ConfigMapVolumeSource(name="kube-root-ca.crt"),
            )
        ],
        volume_mounts=[
            k8s.V1VolumeMount(
                name="cluster-ca",
                mount_path="/etc/ssl/k3s/ca.crt",
                sub_path="ca.crt",
                read_only=True,
            )
        ],
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "512Mi"},
            limits={"cpu": "2", "memory": "1536Mi"},
        ),
        security_context=k8s.V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=1001,
            run_as_group=1001,
            fs_group=1001,
            seccomp_profile=k8s.V1SeccompProfile(type="RuntimeDefault"),
        ),
        container_security_context=k8s.V1SecurityContext(
            allow_privilege_escalation=False,
            run_as_non_root=True,
            read_only_root_filesystem=False,  # mlflow/boto3 write to /tmp
            capabilities=k8s.V1Capabilities(drop=["ALL"]),
        ),
        get_logs=True,
        on_finish_action="delete_pod",
        startup_timeout_seconds=300,
    )


insurance_retention_train()
