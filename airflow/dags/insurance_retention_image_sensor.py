"""insurance_retention_image_sensor -- code-driven CT trigger via an Airflow Asset.

Polls GHCR for the digest behind insurance-retention:latest. When it changes (CI
published a new training-relevant build), it records the new digest ref in the
Airflow Variable `ir_target_image` and UPDATES the Asset `insurance_retention_image`.
The training DAG is scheduled on that Asset (`schedule=[Asset]`), so Airflow triggers
it automatically -- data-aware scheduling. The image -> training dependency then
shows up in Airflow's Asset/lineage graph instead of an imperative
TriggerDagRunOperator.

Design notes:
- The digest rides in the `ir_target_image` Variable (read by the training DAG's
  resolve_image task), not the asset-event extra -- robust templating, no fragile
  context plumbing. The Asset update is purely the trigger signal.
- Watermark `ir_last_trained_image` is advanced on DETECTION (fire once per new
  digest). A failed retrain is re-fired by re-triggering, not automatically -- an
  acceptable trade given promotion is gated.
- Anonymous registry read (public package, no token). Active on creation
  (is_paused_upon_creation=False) so it actually polls on a fresh deploy.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from airflow.sdk import dag, task, Variable, Asset
from airflow.sdk.exceptions import AirflowSkipException

REGISTRY = "ghcr.io"
REPO = "sdr3078/insurance-retention"
IMAGE = f"{REGISTRY}/{REPO}"
TAG = "latest"
VAR_LAST_TRAINED = "ir_last_trained_image"
VAR_TARGET_IMAGE = "ir_target_image"

# The Asset the training DAG is scheduled on. Keyed by name + uri -- the training
# DAG declares an identical Asset(...) to consume it.
IMAGE_ASSET = Asset(name="insurance_retention_image", uri=f"ghcr://{REPO}:{TAG}")

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
    # Active on creation: a cron-driven sensor is useless while paused, and new
    # DAGs are paused by default. Without this a fresh deploy silently never fires.
    is_paused_upon_creation=False,
    tags=["insurance-retention", "ml", "ct-trigger"],
    doc_md=__doc__,
)
def insurance_retention_image_sensor():
    @task(outlets=[IMAGE_ASSET])
    def detect_and_signal() -> str:
        """On a new digest: record the ref + advance the watermark; success updates
        the Asset, which triggers the training DAG. Skip if nothing new."""
        digest = _resolve_latest_digest()
        if digest == Variable.get(VAR_LAST_TRAINED, default=""):
            raise AirflowSkipException(f"No new image; already trained {digest}")
        Variable.set(VAR_TARGET_IMAGE, f"{IMAGE}@{digest}")
        Variable.set(VAR_LAST_TRAINED, digest)
        return digest

    detect_and_signal()


insurance_retention_image_sensor()
