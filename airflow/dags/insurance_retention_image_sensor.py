"""insurance_retention_image_sensor -- code-driven CT trigger.

Polls GHCR for the digest behind insurance-retention:latest. When it changes (CI
published a new training-relevant build), this fires insurance_retention_train on
that *immutable digest* -- so the run is reproducible and the candidate bundle is
stamped with the exact image + commit that produced it (lineage). Promotion stays
the separate gated step; a retrain only ever registers a candidate.

Why poll instead of a webhook / CI push: the GHCR package is public, so the digest
is readable anonymously -- no registry token, no inbound webhook, no cross-repo CI
credential (the same no-external-token stance as ArgoCD Image Updater on the
serving side). Training is not latency-sensitive, so a 15-minute poll is plenty.

State: the last-trained digest lives in the Airflow Variable 'ir_last_trained_image',
advanced only after a triggered run SUCCEEDS, so a failed retrain re-fires next poll.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from airflow.sdk import dag, task, Variable
from airflow.sdk.exceptions import AirflowSkipException
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

REGISTRY = "ghcr.io"
REPO = "sdr3078/insurance-retention"
IMAGE = f"{REGISTRY}/{REPO}"
TAG = "latest"
VAR_LAST_TRAINED = "ir_last_trained_image"

# Accept manifest-index + single-manifest media types so HEAD returns a digest
# whether the tag is a multi-arch index or a single image.
_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def _resolve_latest_digest() -> str:
    """Return the immutable digest the :latest tag currently points to (anonymous read)."""
    with urllib.request.urlopen(
        f"https://{REGISTRY}/token?scope=repository:{REPO}:pull&service={REGISTRY}",
        timeout=30,
    ) as resp:
        token = json.load(resp)["token"]
    req = urllib.request.Request(
        f"https://{REGISTRY}/v2/{REPO}/manifests/{TAG}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": _ACCEPT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        digest = resp.headers.get("Docker-Content-Digest", "")
    if not digest:
        raise ValueError("GHCR returned no Docker-Content-Digest header")
    return digest


@dag(
    dag_id="insurance_retention_image_sensor",
    start_date=datetime(2026, 5, 25),
    schedule="*/15 * * * *",
    catchup=False,
    max_active_runs=1,
    tags=["insurance-retention", "ml", "ct-trigger"],
    doc_md=__doc__,
)
def insurance_retention_image_sensor():
    @task
    def detect_new_image() -> str:
        """Return the latest digest, or skip the run if it is already trained."""
        digest = _resolve_latest_digest()
        if digest == Variable.get(VAR_LAST_TRAINED, default=""):
            raise AirflowSkipException(f"No new image; already trained {digest}")
        return digest

    digest = detect_new_image()

    # conf is a templated field: the {{ }} renders the digest pushed by detect.
    trigger = TriggerDagRunOperator(
        task_id="trigger_training",
        trigger_dag_id="insurance_retention_train",
        conf={"image": IMAGE + "@{{ ti.xcom_pull(task_ids='detect_new_image') }}"},
        wait_for_completion=True,
        poke_interval=30,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    @task
    def mark_trained(digest: str) -> None:
        """Advance the watermark only after training succeeded."""
        Variable.set(VAR_LAST_TRAINED, digest)

    digest >> trigger >> mark_trained(digest)


insurance_retention_image_sensor()
