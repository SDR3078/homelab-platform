#!/usr/bin/env bash
#
# seal-secrets.sh — one-shot ceremony that seals ALL FOUR batzbakbot credentials
# into GitOps. No secret ever touches disk-plaintext, argv, or shell history:
# every value is read from a SILENT prompt and piped straight into kubeseal.
# This script holds no secret and is safe to commit.
#
# Produces (encrypted; safe for the PUBLIC repo):
#   charts/argocd/batzbakbot-repo-sealed.yaml       argocd ns     — ArgoCD clones the private repo
#   charts/batzbakbot/ghcr-pull-sealed.yaml         batzbakbot ns — kubelet pulls the private image
#   charts/batzbakbot/batzbakbot-config-sealed.yaml batzbakbot ns — the real config.yaml
#   charts/batzbakbot/batzbakbot-secrets-sealed.yaml batzbakbot ns — proxy creds + admin phone
#
# WHY config.yaml is sealed and not a ConfigMap: it carries a real name, address,
# birthday, e-mail and phone, and once AUTO_PURCHASE is on those exact values are
# what gets submitted to a real ticket order. This repo is PUBLIC.
#
# WHY the proxy credentials are sealed rather than passed inline: for IPRoyal the
# geo/session targeting lives INSIDE the password (`_country-be`,
# `_session-...`), so the whole connection string is a secret. It was disclosed
# once by dumping a state file that stored it verbatim; the bot now stores only a
# digest, and it must not be re-introduced here in plaintext.
#
# Run it on your devbox (needs kubectl context on the cluster + kubeseal):
#   bash charts/batzbakbot/seal-secrets.sh
#
set -euo pipefail

# --- config ------------------------------------------------------------------
REPO_DIR="${HOMELAB_DIR:-$HOME/homelab-platform}"
BOT_DIR="${BATZBAKBOT_DIR:-$HOME/batzbakbot}"
CONTROLLER_NAME="sealed-secrets-controller"
CONTROLLER_NS="sealed-secrets"

GH_USER="SDR3078"
GH_EMAIL="sanderdereu95@gmail.com"
REPO_URL="https://github.com/SDR3078/batzbakbot.git"
APP_NS="batzbakbot"

REPO_SEALED="charts/argocd/batzbakbot-repo-sealed.yaml"
GHCR_SEALED="charts/batzbakbot/ghcr-pull-sealed.yaml"
CONFIG_SEALED="charts/batzbakbot/batzbakbot-config-sealed.yaml"
APP_SEALED="charts/batzbakbot/batzbakbot-secrets-sealed.yaml"
# -----------------------------------------------------------------------------

cd "$REPO_DIR"
for bin in kubectl kubeseal curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not in PATH." >&2; exit 1; }
done
trap 'unset REPO_PAT GHCR_PAT PROXY_CHECK PROXY_BUY ADMIN_PHONE 2>/dev/null || true' EXIT

seal() { kubeseal --controller-name="$CONTROLLER_NAME" --controller-namespace="$CONTROLLER_NS" -o yaml; }

# === 1/4 · ArgoCD repo-clone credential (fine-grained PAT, Contents: Read) ====
printf '[1/4] GitHub token for ArgoCD repo clone (fine-grained, Contents:Read on batzbakbot): '
read -rs REPO_PAT; echo
[[ -n "${REPO_PAT:-}" ]] || { echo "ERROR: empty token." >&2; exit 1; }
echo "      validating against api.github.com ..."
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $REPO_PAT" \
        -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/rate_limit || echo 000)"
[[ "$code" == "200" ]] || { echo "ERROR: token did not authenticate (HTTP $code). Nothing sealed." >&2; exit 1; }
echo "      OK — authenticates."
kubectl create secret generic batzbakbot-repo -n argocd \
  --from-literal=type=git --from-literal=url="$REPO_URL" \
  --from-literal=username="$GH_USER" --from-literal=password="$REPO_PAT" \
  --dry-run=client -o yaml \
| kubectl label --local -f - -o yaml argocd.argoproj.io/secret-type=repository \
| seal > "$REPO_SEALED"
echo "      -> $REPO_SEALED"

# === 2/4 · GHCR image-pull credential (classic read:packages recommended) =====
printf '[2/4] GitHub token for GHCR image pull (classic read:packages; paste #1 again to try one token): '
read -rs GHCR_PAT; echo
[[ -n "${GHCR_PAT:-}" ]] || { echo "ERROR: empty token." >&2; exit 1; }
kubectl create secret docker-registry ghcr-pull -n "$APP_NS" \
  --docker-server=ghcr.io --docker-username="$GH_USER" \
  --docker-password="$GHCR_PAT" --docker-email="$GH_EMAIL" \
  --dry-run=client -o yaml \
| seal > "$GHCR_SEALED"
echo "      -> $GHCR_SEALED"

# === 3/4 · the real config.yaml ==============================================
# Read from the file rather than a prompt — it is multi-line YAML — but never
# copied anywhere: --from-file streams it straight into the sealing pipe.
CONFIG_SRC="${BATZBAKBOT_CONFIG:-$BOT_DIR/config.yaml}"
echo "[3/4] config.yaml from: $CONFIG_SRC"
[[ -r "$CONFIG_SRC" ]] || { echo "ERROR: cannot read $CONFIG_SRC (set BATZBAKBOT_CONFIG)." >&2; exit 1; }
# A config missing the purchase fields would deploy a bot that reserves a real
# ticket and then dies on the form, so refuse it here rather than at 03:00.
for key in first_name last_name e_mail phone url street house_number zip city country_iso gender birthday; do
  grep -qE "^[[:space:]]*-?[[:space:]]*${key}:[[:space:]]*[^[:space:]#]" "$CONFIG_SRC" \
    || { echo "ERROR: '$key' missing or empty in $CONFIG_SRC. Nothing sealed." >&2; exit 1; }
done
echo "      all purchase fields present."
kubectl create secret generic batzbakbot-config -n "$APP_NS" \
  --from-file=config.yaml="$CONFIG_SRC" \
  --dry-run=client -o yaml \
| seal > "$CONFIG_SEALED"
echo "      -> $CONFIG_SEALED"

# === 4/4 · proxy credentials + admin phone ===================================
# PROXY_CHECK carries the poll traffic and SHOULD rotate freely. PROXY_BUY
# carries a purchase and MUST be sticky: one order spans an overview load, a
# reservation and a form submit bound to a single PHPSESSID, so an exit that
# rotates mid-flow breaks the session after a real ticket is already held.
printf '[4/4] PROXY_CHECK (poll egress, scheme://user:pass@host:port; empty = direct): '
read -rs PROXY_CHECK; echo
printf '      PROXY_BUY   (purchase egress, MUST carry _session- and _lifetime-): '
read -rs PROXY_BUY; echo
printf '      ADMIN_PHONE (WARNING+ alerts, digits only, no + or 00; empty = off): '
read -r ADMIN_PHONE
if [[ -n "${PROXY_BUY:-}" && "$PROXY_BUY" != *"_session-"* ]]; then
  echo "ERROR: PROXY_BUY has no _session- tag. A rotating buy egress breaks a purchase" >&2
  echo "       mid-order, after the ticket is already held. Nothing sealed." >&2
  exit 1
fi
if [[ -z "${PROXY_CHECK:-}" ]]; then
  echo "      NOTE: PROXY_CHECK empty — the bot will poll DIRECT from the cluster's"
  echo "            address. That is the address that got filtered on 2026-08-04."
fi
kubectl create secret generic batzbakbot-secrets -n "$APP_NS" \
  --from-literal=PROXY_CHECK="$PROXY_CHECK" \
  --from-literal=PROXY_BUY="$PROXY_BUY" \
  --from-literal=ADMIN_PHONE="$ADMIN_PHONE" \
  --dry-run=client -o yaml \
| seal > "$APP_SEALED"
echo "      -> $APP_SEALED"

# --- sanity: each sealed file carries the expected encrypted keys -------------
grep -q 'password'         "$REPO_SEALED"   || { echo "ERROR: repo seal incomplete." >&2; exit 1; }
grep -q 'dockerconfigjson' "$GHCR_SEALED"   || { echo "ERROR: ghcr seal incomplete." >&2; exit 1; }
grep -q 'config.yaml'      "$CONFIG_SEALED" || { echo "ERROR: config seal incomplete." >&2; exit 1; }
for k in PROXY_CHECK PROXY_BUY ADMIN_PHONE; do
  grep -q "$k" "$APP_SEALED" || { echo "ERROR: $k missing from $APP_SEALED." >&2; exit 1; }
done
# Nothing plaintext must ever survive in these files.
if grep -RqE '^[[:space:]]*(stringData|data):' "$REPO_SEALED" "$GHCR_SEALED" "$CONFIG_SEALED" "$APP_SEALED"; then
  echo "ERROR: a plaintext data/stringData block survived sealing. DO NOT COMMIT." >&2
  exit 1
fi

cat <<'EOF'

Sealed. NEXT STEPS

  1. Review the four files, then commit + push them. They are encrypted to THIS
     cluster's controller key and are useless to anyone else.

  2. Push charts/argocd/batzbakbot-project.yaml FIRST (or at least in the same
     push): the two Applications name `project: batzbakbot`, and a sync against a
     project that does not exist yet is rejected.

  3. The image must exist before the workload syncs. Push the app repo so its
     CI builds and bumps k8s/kustomization.yaml — until then the tag is
     `bootstrap`, which nothing publishes, and the pod ImagePullBackOffs on
     purpose rather than deploying something unintended.

  4. Pair WhatsApp before turning AUTO_PURCHASE on. The API is ClusterIP-only:
       kubectl -n batzbakbot port-forward svc/whatsapp 3000:3000
       curl -X POST localhost:3000/devices -H 'Content-Type: application/json' -d '{"name":"batzbakbot"}'
     then open http://localhost:3000 and pair. Register EXACTLY ONE device — the
     server defaults to it, which is why the bot sends no X-Device-Id; a second
     device makes every notification 400.
EOF
