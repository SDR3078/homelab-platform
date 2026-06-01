"""insurance_retention_tune -- offline hyperparameter search on REAL bronze.train.

The slow-cadence tuning step (Phase 2 of the tuning lifecycle): a KubernetesPodOperator launches the
insurance-retention image and runs `tune.py --register`, which runs the nested-CV Optuna search on the
REAL bronze.train and REGISTERS the result as a new `insurance-retention-params` version in MLflow
(UN-aliased). It does NOT activate it -- `insurance_retention_promote_params` sets the @production
alias, and the train DAG's init container resolves that alias into BEST_PARAMS_PATH. So the cluster
refits its OWN real-data-tuned params; the image-baked synthetic params are only the demo default.

Cadence: MANUAL (schedule=None), like promotion. Re-tuning is EXCEPTIONAL -- a schema change, a
data-volume step, or a sustained NV decline -- NOT every retrain (that would ~130x the compute and
break the gate's data-vs-params attribution). Trigger from the UI / `airflow dags trigger`, optionally
`-c '{"n_trials": 50, "snapshot_id": "<id>"}'`.

Pod wiring mirrors the train DAG (reads bronze.train @ a pinned snapshot, logs/registers to MLflow):
MLflow tracking + MinIO artifact creds, Lakekeeper catalog + LAKE_S3 read creds, and IR_DATA_PROFILE=real
so the registered version is tagged real (promote_params refuses to ship non-real params by default).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow.sdk import Asset, Variable, dag, get_current_context, task
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"

# Read (not consumed as a schedule): the bronze table the search runs on, for the lineage graph.
BRONZE_ASSET = Asset(name="insurance_retention_bronze", uri="iceberg://demo/insurance_retention.bronze.train")


@dag(
    dag_id="insurance_retention_tune",
    start_date=datetime(2026, 6, 1),
    # Manual + deliberate: tuning is NOT data-aware -- it must stay decoupled from the CT retrain
    # cadence (the train DAG's image|bronze trigger). Re-tune on exception, by hand.
    schedule=None,
    catchup=False,
    is_paused_upon_creation=False,
    dagrun_timeout=timedelta(hours=2),  # the nested-CV search is wall-clock-bound (n_jobs=1 for determinism)
    tags=["insurance-retention", "ml", "tuning"],
    doc_md=__doc__,
)
def insurance_retention_tune():
    @task
    def resolve_image() -> str:
        """Pick the image: manual conf override, else the sensor's target, else :latest."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return conf.get("image") or Variable.get("ir_target_image", default=IMAGE)

    @task
    def resolve_data_snapshot() -> str:
        """Pin the bronze snapshot to tune on: conf override, else the last-landed bronze snapshot."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return str(conf.get("snapshot_id") or Variable.get("ir_bronze_snapshot_id", default="unknown"))

    image = resolve_image()
    snapshot = resolve_data_snapshot()

    tune = KubernetesPodOperator(
        task_id="tune_and_register",
        name="insurance-retention-tune",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        # Module form (-m) so /app is on sys.path for the repo-root imports (lakehouse), like train.
        cmds=["python", "-m", "training.tune"],
        # --register: publish a new insurance-retention-params version (un-aliased). --output is a
        # throwaway pod path (the artifact lands in MLflow; the baked /app/artifacts copy is read-only).
        arguments=[
            "--register",
            "--n-trials", "{{ (dag_run.conf or {}).get('n_trials', 50) }}",
            "--output", "/tmp/best_params.json",
        ],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            "LAKEKEEPER_URI": "http://lakekeeper.lakekeeper.svc.cluster.local:8181/catalog",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
            # The registered params version is tagged data=real -> promote_params ships it without
            # --allow-synthetic. (The baked default is synthetic; this run tunes the REAL bronze.train.)
            "IR_DATA_PROFILE": "real",
            "IR_IMAGE_REF": "{{ ti.xcom_pull(task_ids='resolve_image') }}",  # lineage: code+image
            "IR_DATA_SNAPSHOT_ID": "{{ ti.xcom_pull(task_ids='resolve_data_snapshot') }}",  # lineage: data
        },
        secrets=[
            # MLflow artifact store (MinIO) -- where the registered params artifact is written.
            Secret("env", "AWS_ACCESS_KEY_ID", "insurance-retention-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "AWS_SECRET_ACCESS_KEY", "insurance-retention-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
            # Lakehouse read (bronze.train @ pinned snapshot) -- the lakekeeper svcacct.
            Secret("env", "LAKE_S3_ACCESS_KEY_ID", "lakekeeper-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "LAKE_S3_SECRET_ACCESS_KEY", "lakekeeper-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
        ],
        volumes=[
            k8s.V1Volume(name="cluster-ca", config_map=k8s.V1ConfigMapVolumeSource(name="kube-root-ca.crt")),
        ],
        volume_mounts=[
            k8s.V1VolumeMount(name="cluster-ca", mount_path="/etc/ssl/k3s/ca.crt", sub_path="ca.crt", read_only=True),
        ],
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "1", "memory": "1Gi"},
            limits={"cpu": "2", "memory": "2Gi"},
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
            read_only_root_filesystem=False,  # optuna/lightgbm/mlflow write to /tmp
            capabilities=k8s.V1Capabilities(drop=["ALL"]),
        ),
        get_logs=True,
        on_finish_action="delete_pod",
        startup_timeout_seconds=300,
    )

    [image, snapshot] >> tune


insurance_retention_tune()
