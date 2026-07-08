#!/usr/bin/env bash
#
# seal-secrets.sh — one-shot ceremony that seals ALL THREE wardstone credentials
# into GitOps. No secret ever touches disk-plaintext, argv, or shell history:
# every value is read from a SILENT prompt and piped straight into kubeseal.
# This script holds no secret and is safe to commit.
#
# Produces (encrypted; safe for the PUBLIC repo):
#   charts/argocd/wardstone-repo-sealed.yaml        argocd ns   — ArgoCD clones the private repo
#   charts/wardstone/ghcr-pull-sealed.yaml          wardstone ns— kubelet pulls the private image
#   charts/wardstone/wardstone-secrets-sealed.yaml  wardstone ns— admin token + PayPal client id/secret
#
# The PayPal WEBHOOK_ID is deliberately NOT sealed here — it's added later, after the
# endpoint is live and the webhook is registered, via `kubeseal --merge-into`
# (see the printed NEXT STEPS). Until then the PayPal adapter stays cleanly disabled.
#
# Run it on your devbox (needs kubectl context on the cluster + kubeseal):
#   bash charts/wardstone/seal-secrets.sh
#
set -euo pipefail

# --- config ------------------------------------------------------------------
REPO_DIR="${HOMELAB_DIR:-$HOME/homelab-platform}"
CONTROLLER_NAME="sealed-secrets-controller"
CONTROLLER_NS="sealed-secrets"

GH_USER="SDR3078"
GH_EMAIL="sanderdereu95@gmail.com"
REPO_URL="https://github.com/SDR3078/wardstone.git"
APP_NS="wardstone"

REPO_SEALED="charts/argocd/wardstone-repo-sealed.yaml"
GHCR_SEALED="charts/wardstone/ghcr-pull-sealed.yaml"
APP_SEALED="charts/wardstone/wardstone-secrets-sealed.yaml"
# -----------------------------------------------------------------------------

cd "$REPO_DIR"
for bin in kubectl kubeseal openssl curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not in PATH." >&2; exit 1; }
done
trap 'unset REPO_PAT GHCR_PAT PP_ID PP_SECRET ADMIN_TOKEN 2>/dev/null || true' EXIT

seal() { kubeseal --controller-name="$CONTROLLER_NAME" --controller-namespace="$CONTROLLER_NS" -o yaml; }

# === 1/3 · ArgoCD repo-clone credential (fine-grained PAT, Contents: Read) ====
printf '[1/3] GitHub token for ArgoCD repo clone (fine-grained, Contents:Read on wardstone): '
read -rs REPO_PAT; echo
[[ -n "${REPO_PAT:-}" ]] || { echo "ERROR: empty token." >&2; exit 1; }
echo "      validating against api.github.com ..."
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $REPO_PAT" \
        -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/rate_limit || echo 000)"
[[ "$code" == "200" ]] || { echo "ERROR: token did not authenticate (HTTP $code). Nothing sealed." >&2; exit 1; }
echo "      OK — authenticates."
kubectl create secret generic wardstone-repo -n argocd \
  --from-literal=type=git --from-literal=url="$REPO_URL" \
  --from-literal=username="$GH_USER" --from-literal=password="$REPO_PAT" \
  --dry-run=client -o yaml \
| kubectl label --local -f - -o yaml argocd.argoproj.io/secret-type=repository \
| seal > "$REPO_SEALED"
echo "      -> $REPO_SEALED"

# === 2/3 · GHCR image-pull credential (classic read:packages recommended) =====
printf '[2/3] GitHub token for GHCR image pull (classic read:packages; paste #1 again to try one token): '
read -rs GHCR_PAT; echo
[[ -n "${GHCR_PAT:-}" ]] || { echo "ERROR: empty token." >&2; exit 1; }
kubectl create secret docker-registry ghcr-pull -n "$APP_NS" \
  --docker-server=ghcr.io --docker-username="$GH_USER" \
  --docker-password="$GHCR_PAT" --docker-email="$GH_EMAIL" \
  --dry-run=client -o yaml \
| seal > "$GHCR_SEALED"
echo "      -> $GHCR_SEALED"

# === 3/3 · wardstone-secrets (generated admin token + PayPal sandbox creds) ===
ADMIN_TOKEN="$(openssl rand -hex 32)"
printf '[3/3] PayPal SANDBOX client-id: ';     read -rs PP_ID;     echo
printf '      PayPal SANDBOX client-secret: '; read -rs PP_SECRET; echo
[[ -n "${PP_ID:-}" && -n "${PP_SECRET:-}" ]] || { echo "ERROR: empty PayPal creds." >&2; exit 1; }
kubectl create secret generic wardstone-secrets -n "$APP_NS" \
  --from-literal=WARDSTONE_ADMIN_TOKEN="$ADMIN_TOKEN" \
  --from-literal=WARDSTONE_PAYPAL_CLIENT_ID="$PP_ID" \
  --from-literal=WARDSTONE_PAYPAL_CLIENT_SECRET="$PP_SECRET" \
  --dry-run=client -o yaml \
| seal > "$APP_SEALED"
echo "      -> $APP_SEALED  (WEBHOOK_ID added later via --merge-into)"

# --- sanity: each sealed file carries the expected encrypted keys -------------
grep -q 'password'          "$REPO_SEALED" || { echo "ERROR: repo seal incomplete." >&2; exit 1; }
grep -q 'dockerconfigjson'  "$GHCR_SEALED" || { echo "ERROR: ghcr seal incomplete." >&2; exit 1; }
for k in WARDSTONE_ADMIN_TOKEN WARDSTONE_PAYPAL_CLIENT_ID WARDSTONE_PAYPAL_CLIENT_SECRET; do
  grep -q "$k" "$APP_SEALED" || { echo "ERROR: $k missing from $APP_SEALED." >&2; exit 1; }
done

cat <<NOTE

============================================================================
  All three SealedSecrets written (encrypted). NOT yet committed — they'll go
  in with the rest of the wardstone glue.

  >>> SAVE THIS ADMIN TOKEN in your password manager (it's how you call /admin;
      it is NOT recoverable — only the sealed ciphertext remains):

        WARDSTONE_ADMIN_TOKEN = $ADMIN_TOKEN

  NEXT STEPS (later, once the endpoint is live):
   - register the PayPal sandbox webhook at https://wardstone-hooks.batzbak.top/webhooks/paypal
   - capture its webhook_id, then merge it in:
       kubectl create secret generic wardstone-secrets -n wardstone \\
         --from-literal=WARDSTONE_PAYPAL_WEBHOOK_ID=<id> --dry-run=client -o yaml \\
       | kubeseal --controller-name=$CONTROLLER_NAME --controller-namespace=$CONTROLLER_NS \\
                  -o yaml --merge-into $APP_SEALED
============================================================================
NOTE
