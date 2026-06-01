"""insurance_retention_scoring -- batch-score the population into gold.selections.

Orchestration only (like the training DAG): a KubernetesPodOperator launches the model image to run
training/score.py, which reads bronze.score@snapshot, loads the @production bundle, runs the shared
build_features + score + select_top_k, and writes per-season gold.selections. Because the bundle
loader (bundle.py) AND the scoring (the insurance_retention wheel) are shared with serving/app.py,
the batch selected set equals the live /select top-K by construction -- "batch == sync".

Trigger (data-engineer + mlops-engineer review, 2026-05-28): schedule = production_asset |
bronze_score_asset (`|` = AssetAny / OR). Re-score when a NEW BUNDLE is promoted to @production (the
dominant trigger; the production Asset is emitted by insurance_retention_promote on a successful,
gated promotion) OR when a NEW SCORE POPULATION lands (insurance_retention_bronze_score_ingest fires
the bronze.score Asset). Both edges are independent and visible in the Asset lineage graph.

resolve_image picks the image (conf override, else the ir_target_image Variable, else :latest);
resolve_score_snapshot pins bronze.score (conf override, else the ir_score_snapshot_id the ingest
published); resolve_season picks the gold partition key (conf override, else 2022) -- NEVER
datetime.now().year, since the scoring season is an explicit business input.

Pod wiring mirrors insurance_retention_train: MLflow tracking + MinIO (insurance-retention creds for
the bundle's artifacts) + Lakekeeper (LAKE_S3_* for the bronze.score READ and the gold WRITE), the
cluster-CA volume, and a PSA 'restricted'-compliant securityContext, in the airflow namespace.
"""

from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task, Variable, Asset, get_current_context
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

IMAGE = "ghcr.io/sdr3078/insurance-retention:latest"
DEFAULT_SEASON = "2022"
TOP_K = "200"  # the case selects exactly 200

# Consumed Assets -- name+uri MUST match the producers (Airflow keys assets by name + uri).
#   production:   insurance_retention_promote.py   (emitted on a successful gated promotion)
#   bronze.score: insurance_retention_bronze_score_ingest.py
PRODUCTION_ASSET = Asset(name="insurance_retention_production", uri="mlflow://insurance-retention-bundle@production")
BRONZE_SCORE_ASSET = Asset(name="insurance_retention_bronze_score", uri="iceberg://demo/insurance_retention.bronze.score")


@dag(
    dag_id="insurance_retention_scoring",
    start_date=datetime(2026, 5, 28),
    # Re-score on a NEW @production bundle (promotion) OR a NEW score population. `|` = AssetAny (OR).
    schedule=PRODUCTION_ASSET | BRONZE_SCORE_ASSET,
    catchup=False,
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "scoring"],
    doc_md=__doc__,
)
def insurance_retention_scoring():
    @task
    def resolve_image() -> str:
        """Image to run: conf override, else the sensor's target, else :latest."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return conf.get("image") or Variable.get("ir_target_image", default=IMAGE)

    @task
    def resolve_score_snapshot() -> str:
        """bronze.score snapshot to PIN: conf override, else the ingest's published id, else unknown
        (score.py then reads the current snapshot, logged loudly)."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return str(conf.get("snapshot_id") or Variable.get("ir_score_snapshot_id", default="unknown"))

    @task
    def resolve_season() -> str:
        """Gold partition key: conf override, else the default season."""
        ctx = get_current_context()
        dag_run = ctx.get("dag_run")
        conf = (dag_run.conf or {}) if dag_run is not None else {}
        return str(conf.get("season", DEFAULT_SEASON))

    image = resolve_image()
    snapshot = resolve_score_snapshot()
    season = resolve_season()

    score = KubernetesPodOperator(
        task_id="score_to_gold",
        name="insurance-retention-score",
        namespace="airflow",
        image="{{ ti.xcom_pull(task_ids='resolve_image') }}",
        image_pull_policy="Always",
        # Run as a module (not a path) so /app (WORKDIR) is on sys.path and score.py's repo-level
        # `import bundle` resolves -- mirrors how serving launches (`uvicorn serving.app:app`).
        # `python /app/training/score.py` would put /app/training on sys.path[0], not /app.
        cmds=["python", "-m", "training.score"],
        arguments=[
            "--season", "{{ ti.xcom_pull(task_ids='resolve_season') }}",
            "--top-k", TOP_K,
        ],
        env_vars={
            "MLFLOW_TRACKING_URI": "http://mlflow.mlflow.svc.cluster.local",
            "MLFLOW_S3_ENDPOINT_URL": "https://minio.data-platform.svc.cluster.local",
            # Iceberg REST catalog (Lakekeeper). Set explicitly: lakehouse.py's default was scrubbed to a
            # localhost placeholder for the public repo and has no fallback to a cluster-set var.
            "LAKEKEEPER_URI": "http://lakekeeper.lakekeeper.svc.cluster.local:8181/catalog",
            "AWS_CA_BUNDLE": "/etc/ssl/k3s/ca.crt",
            "HOME": "/home/appuser",
            "IR_SCORE_SNAPSHOT_ID": "{{ ti.xcom_pull(task_ids='resolve_score_snapshot') }}",  # pin the read
            "IR_IMAGE_REF": "{{ ti.xcom_pull(task_ids='resolve_image') }}",  # lineage echo
        },
        secrets=[
            # MLflow artifact store (MinIO) -- bundle model + median_fill download.
            Secret("env", "AWS_ACCESS_KEY_ID", "insurance-retention-s3-credentials", "AWS_ACCESS_KEY_ID"),
            Secret("env", "AWS_SECRET_ACCESS_KEY", "insurance-retention-s3-credentials", "AWS_SECRET_ACCESS_KEY"),
            # Lakehouse READ (bronze.score) + WRITE (gold.selections) -- the lakekeeper svcacct
            # (the same creds the bronze ingest writes with).
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
            requests={"cpu": "500m", "memory": "512Mi"},
            limits={"cpu": "2", "memory": "1536Mi"},
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

    [image, snapshot, season] >> score


insurance_retention_scoring()
