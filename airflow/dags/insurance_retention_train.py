"""insurance_retention_train -- train + register the model bundle on the cluster MLflow.

Orchestration only: this DAG does NOT run the ML code in an Airflow worker (the
Airflow image has no lightgbm / mlflow / features-wheel). Instead a
KubernetesPodOperator launches the purpose-built train/serve image
(ghcr.io/sdr3078/insurance-retention) which runs `train.py --register`.

Trigger: data-aware scheduling on the `insurance_retention_image` Asset. The
`insurance_retention_image_sensor` DAG updates that Asset when CI publishes a new
image, so Airflow runs this DAG automatically (the image -> training edge is
visible in the Asset/lineage graph). `resolve_image` chooses what to run:
`dag_run.conf['image']` for a manual run, else the `ir_target_image` Variable the
sensor set, else `:latest`. The chosen ref is passed to the pod as IR_IMAGE_REF so
train.py stamps it into the bundle for lineage. A data-driven trigger (Iceberg
bronze ingest, Workstream D) can later update the same/another Asset.

It REGISTERS a new bundle version on the cluster MLflow (Postgres metadata +
MinIO artifacts); it does NOT promote it. Promotion stays a separate, gated step
(promote_bundle.py), so a retrain never auto-ships to production -- the clean
CT-vs-promotion split.

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

from airflow.sdk import dag, task, Variable, Asset, get_current_context
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

# Default image for manual / ad-hoc runs when no target has been resolved.
IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"

# Consumed Asset -- must match the producer in insurance_retention_image_sensor.py
# (Airflow keys assets by name + uri).
IMAGE_ASSET = Asset(name="insurance_retention_image", uri="ghcr://sdr3078/insurance-retention:latest")


@dag(
    dag_id="insurance_retention_train",
    start_date=datetime(2026, 5, 25),
    # Data-aware: runs when the image Asset is updated by the sensor.
    schedule=[IMAGE_ASSET],
    catchup=False,
    # Active on creation: an Asset-scheduled (or triggered) DAG that is paused
    # never runs its triggered/scheduled runs -- they sit queued.
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "training"],
    doc_md=__doc__,
)
def insurance_retention_train():
    @task
    def resolve_image() -> str:
        """Pick the image to run: manual conf override, else the sensor's target, else :latest."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return conf.get("image") or Variable.get("ir_target_image", default=IMAGE)

    image = resolve_image()

    train = KubernetesPodOperator(
        task_id="train_and_register",
        name="insurance-retention-train",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        cmds=["python", "/app/training/train.py"],
        arguments=["--register", "--experiment-name", "insurance-retention"],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
            "IR_IMAGE_REF": "{{ ti.xcom_pull(task_ids='resolve_image') }}",  # lineage
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

    image >> train


insurance_retention_train()
