"""insurance_retention_train -- train + register the model bundle on the cluster MLflow.

Orchestration only: this DAG does NOT run the ML code in an Airflow worker (the
Airflow image has no lightgbm / mlflow / features-wheel). Instead a
KubernetesPodOperator launches the purpose-built train/serve image
(ghcr.io/sdr3078/insurance-retention) which runs `train.py --register`.

Trigger: data-aware scheduling on TWO Assets -- the CT trigger fires on new CODE or
new DATA. `insurance_retention_image_sensor` updates the `insurance_retention_image`
Asset when CI publishes a new image; `insurance_retention_bronze_ingest` updates the
`insurance_retention_bronze` Asset when new training data lands. Either edge runs this
DAG automatically (both are visible in the Asset/lineage graph; `|` = OR, see schedule).
`resolve_image` picks the image (`dag_run.conf['image']`, else the `ir_target_image`
Variable the sensor set, else `:latest`); `resolve_data_snapshot` picks the bronze
snapshot to PIN (`dag_run.conf['snapshot_id']`, else the `ir_bronze_snapshot_id`
Variable the bronze DAG published). Both reach the pod (IR_IMAGE_REF +
IR_DATA_SNAPSHOT_ID) so train.py stamps the full code+image+data lineage triangle.

It REGISTERS a new bundle version on the cluster MLflow (Postgres metadata +
MinIO artifacts); it does NOT promote it. Promotion stays a separate, gated step
(promote_bundle.py), so a retrain never auto-ships to production -- the clean
CT-vs-promotion split.

Pod wiring (mirrors the proven Job):
  - MLFLOW_TRACKING_URI -> in-cluster MLflow Service
  - MinIO creds via the sealed 'insurance-retention-s3-credentials' (this ns) +
    MLFLOW_S3_ENDPOINT_URL + AWS_CA_BUNDLE (cluster CA from kube-root-ca.crt)
  - Lakehouse read creds via 'lakekeeper-s3-credentials' (LAKE_S3_*), for the
    Iceberg bronze.train read at the pinned snapshot
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

# Consumed Assets -- must match the producers (Airflow keys assets by name + uri).
#   image:  insurance_retention_image_sensor.py
#   bronze: insurance_retention_bronze_ingest.py
IMAGE_ASSET = Asset(name="insurance_retention_image", uri="ghcr://sdr3078/insurance-retention:latest")
BRONZE_ASSET = Asset(name="insurance_retention_bronze", uri="iceberg://demo/insurance_retention.bronze.train")


@dag(
    dag_id="insurance_retention_train",
    start_date=datetime(2026, 5, 25),
    # Data-aware CT trigger: retrain on NEW CODE (image Asset, set by the sensor) OR
    # NEW DATA (bronze Asset, set by the bronze-ingest DAG). `|` = AssetAny (OR); a
    # plain list [A, B] would be AssetAll (AND -- wait for BOTH), which is not wanted.
    schedule=IMAGE_ASSET | BRONZE_ASSET,
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

    @task
    def resolve_data_snapshot() -> str:
        """Pick the bronze snapshot id to PIN: manual conf override, else the snapshot
        the bronze-ingest DAG last published (ir_bronze_snapshot_id Variable), else
        'unknown' (train.py then reads the current snapshot, logged loudly). Works for
        both trigger types -- the Variable always holds the latest landed bronze snapshot."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return str(conf.get("snapshot_id") or Variable.get("ir_bronze_snapshot_id", default="unknown"))

    image = resolve_image()
    snapshot = resolve_data_snapshot()

    train = KubernetesPodOperator(
        task_id="train_and_register",
        name="insurance-retention-train",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        # Run as a MODULE (-m), matching the scoring DAG. `python -m pipelines.train` puts the
        # image WORKDIR (/app) on sys.path, so train.py's `from runtime.lakehouse import
        # build_catalog` resolves. `python /app/pipelines/train.py` would put only /app/pipelines on
        # the path and break that import (ModuleNotFoundError: No module named 'runtime').
        cmds=["python", "-m", "pipelines.train"],
        arguments=["--register", "--experiment-name", "insurance-retention"],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            # Iceberg REST catalog (Lakekeeper). MUST be set explicitly: lakehouse.py's default was
            # scrubbed to a localhost placeholder for the public repo, and unlike the S3 endpoint it has
            # no fallback to a cluster-set var -- so the in-cluster URL has to live here (as serving does).
            "LAKEKEEPER_URI": "http://lakekeeper.lakekeeper.svc.cluster.local:8181/catalog",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
            "IR_IMAGE_REF": "{{ ti.xcom_pull(task_ids='resolve_image') }}",  # lineage: code+image
            "IR_DATA_SNAPSHOT_ID": "{{ ti.xcom_pull(task_ids='resolve_data_snapshot') }}",  # lineage: data leg
            # The resolve-params init container writes the @production tuned params here (or, on first
            # run before any tune is promoted, leaves the image-baked synthetic default in place).
            "BEST_PARAMS_PATH": "/params/best_params.json",
        },
        secrets=[
            # MLflow artifact store (MinIO) -- model upload/download.
            Secret("env", "AWS_ACCESS_KEY_ID", "insurance-retention-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "AWS_SECRET_ACCESS_KEY", "insurance-retention-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
            # Lakehouse read (Iceberg data files in iceberg-warehouse) -- the purpose-built
            # lakekeeper svcacct; the shared lakehouse.build_catalog() prefers LAKE_S3_* over AWS_*.
            # Injected into THIS pod only (serving's Deployment is a separate spec).
            Secret("env", "LAKE_S3_ACCESS_KEY_ID", "lakekeeper-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "LAKE_S3_SECRET_ACCESS_KEY", "lakekeeper-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
        ],
        volumes=[
            k8s.V1Volume(
                name="cluster-ca",
                config_map=k8s.V1ConfigMapVolumeSource(name="kube-root-ca.crt"),
            ),
            # Shared by the resolve-params init container (writes) + the train container (reads).
            k8s.V1Volume(name="params", empty_dir=k8s.V1EmptyDirVolumeSource()),
        ],
        volume_mounts=[
            k8s.V1VolumeMount(
                name="cluster-ca",
                mount_path="/etc/ssl/k3s/ca.crt",
                sub_path="ca.crt",
                read_only=True,
            ),
            k8s.V1VolumeMount(name="params", mount_path="/params", read_only=True),
        ],
        # Init container: resolve the @production tuned params into /params BEFORE train.py runs, so the
        # CT refits the cluster's REAL-data params. On first run (no @production alias yet) resolve_params
        # exits 0 without writing and train.py reads the image-baked synthetic default. Uses :latest
        # (resolve_params is stable) + only MLflow/MinIO creds (it reads the registry, not the lakehouse).
        init_containers=[
            k8s.V1Container(
                name="resolve-params",
                image=IMAGE,
                image_pull_policy="Always",
                command=["python", "-m", "pipelines.resolve_params"],
                args=["--output", "/params/best_params.json"],
                env=[
                    k8s.V1EnvVar(name="MLFLOW_TRACKING_URI", value="http://mlflow.mlflow.svc.cluster.local"),
                    k8s.V1EnvVar(name="MLFLOW_S3_ENDPOINT_URL", value="https://minio.data-platform.svc.cluster.local"),
                    k8s.V1EnvVar(name="AWS_CA_BUNDLE", value="/etc/ssl/k3s/ca.crt"),
                    k8s.V1EnvVar(name="HOME", value="/home/appuser"),
                    k8s.V1EnvVar(
                        name="AWS_ACCESS_KEY_ID",
                        value_from=k8s.V1EnvVarSource(
                            secret_key_ref=k8s.V1SecretKeySelector(
                                name="insurance-retention-s3-credentials", key="AWS_ACCESS_KEY_ID"
                            )
                        ),
                    ),
                    k8s.V1EnvVar(
                        name="AWS_SECRET_ACCESS_KEY",
                        value_from=k8s.V1EnvVarSource(
                            secret_key_ref=k8s.V1SecretKeySelector(
                                name="insurance-retention-s3-credentials", key="AWS_SECRET_ACCESS_KEY"
                            )
                        ),
                    ),
                ],
                volume_mounts=[
                    k8s.V1VolumeMount(
                        name="cluster-ca", mount_path="/etc/ssl/k3s/ca.crt", sub_path="ca.crt", read_only=True
                    ),
                    k8s.V1VolumeMount(name="params", mount_path="/params"),
                ],
                security_context=k8s.V1SecurityContext(
                    allow_privilege_escalation=False,
                    run_as_non_root=True,
                    run_as_user=1001,
                    read_only_root_filesystem=False,
                    capabilities=k8s.V1Capabilities(drop=["ALL"]),
                ),
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

    [image, snapshot] >> train


insurance_retention_train()
