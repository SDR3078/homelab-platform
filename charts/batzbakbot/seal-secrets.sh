#!/usr/bin/env bash
#
# seal-secrets.sh — one-shot ceremony that seals ALL FIVE batzbakbot credentials
# into GitOps. No secret ever touches disk-plaintext, argv, or shell history:
# every value is read from a SILENT prompt and piped straight into kubeseal.
# This script holds no secret and is safe to commit.
#
# Produces (encrypted; safe for the PUBLIC repo):
#   charts/argocd/batzbakbot-repo-sealed.yaml       argocd ns     — ArgoCD clones the private repo
#   charts/batzbakbot/ghcr-pull-sealed.yaml         batzbakbot ns — kubelet pulls the private image
#   charts/batzbakbot/batzbakbot-config-sealed.yaml batzbakbot ns — the real config.yaml
#   charts/batzbakbot/batzbakbot-secrets-sealed.yaml batzbakbot ns — proxy creds + admin phone
#   charts/batzbakbot/whatsapp-basic-auth-sealed.yaml batzbakbot ns — gates the whatsapp Ingress
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
AUTH_SEALED="charts/batzbakbot/whatsapp-basic-auth-sealed.yaml"
# -----------------------------------------------------------------------------

cd "$REPO_DIR"
for bin in kubectl kubeseal curl openssl; do
  command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not in PATH." >&2; exit 1; }
done
trap 'unset REPO_PAT GHCR_PAT PROXY_CHECK PROXY_BUY ADMIN_PHONE WA_PASS WA_PASS2 WA_HASH 2>/dev/null || true' EXIT

seal() { kubeseal --controller-name="$CONTROLLER_NAME" --controller-namespace="$CONTROLLER_NS" -o yaml; }

# === 1/5 · ArgoCD repo-clone credential (fine-grained PAT, Contents: Read) ====
printf '[1/5] GitHub token for ArgoCD repo clone (fine-grained, Contents:Read on batzbakbot): '
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

# === 2/5 · GHCR image-pull credential (classic read:packages) ================
# REUSE the token already behind wardstone/portfolio-site/wedding's ghcr-pull.
# GHCR tokens are user-wide, not repository-scoped, so the existing one already
# pulls this image -- verified against the registry: an authenticated HEAD of
# ghcr.io/v2/sdr3078/batzbakbot/manifests/latest returned 200 with it.
#
# It still needs its own sealed copy here because Secrets are namespaced and
# this platform seals ghcr-pull per namespace rather than reflecting it.
# Reflector is in use, but for cluster-GENERATED secrets (the CNPG credentials,
# the wildcard cert) -- a credential declared in Git should not depend on a
# controller mirroring it at runtime.
#
# Contrast with [1/5]: that one is FINE-GRAINED and carries an explicit
# repository list, so wardstone's cannot be reused -- it answers 404 for
# batzbakbot (GitHub returns 404 rather than 403 so as not to confirm a private
# repo exists). Either add batzbakbot to an existing fine-grained token's list
# or mint a new one.
echo "[2/5] GHCR image-pull credential."
echo "      Press ENTER with no input to COPY the existing ghcr-pull from another"
echo "      namespace (${GHCR_SOURCE_NS:-wardstone}) instead of retyping the token."
printf '      GHCR token (classic, read:packages), or ENTER to copy: '
read -rs GHCR_PAT; echo

if [[ -z "${GHCR_PAT:-}" ]]; then
  # The credential is already in the cluster. Re-key it into this namespace
  # rather than asking for a value nobody has written down: the whole Secret is
  # transformed in a pipe and re-sealed, so the token is never displayed, never
  # reaches argv, and never lands on disk in plaintext. A forgotten token is a
  # bad reason to mint a second one -- more copies of a credential is strictly
  # worse than reusing the one that already works.
  SRC_NS="${GHCR_SOURCE_NS:-wardstone}"
  echo "      copying $SRC_NS/ghcr-pull -> $APP_NS/ghcr-pull (value never shown)"
  kubectl -n "$SRC_NS" get secret ghcr-pull -o json >/dev/null 2>&1 \
    || { echo "ERROR: no ghcr-pull in namespace '$SRC_NS'. Set GHCR_SOURCE_NS." >&2; exit 1; }

  # Verify the COPIED credential can pull this image before sealing it, exactly
  # as the typed path does. Decoded inside python and never echoed.
  echo "      verifying it can pull ghcr.io/sdr3078/batzbakbot ..."
  kubectl -n "$SRC_NS" get secret ghcr-pull -o json | python3 -c '
import base64, json, sys, urllib.request
src = json.load(sys.stdin)
auths = json.loads(base64.b64decode(src["data"][".dockerconfigjson"]))["auths"]["ghcr.io"]
if "auth" in auths:
    user, _, pw = base64.b64decode(auths["auth"]).decode().partition(":")
else:
    user, pw = auths.get("username", ""), auths.get("password", "")
basic = base64.b64encode(f"{user}:{pw}".encode()).decode()
req = urllib.request.Request(
    "https://ghcr.io/token?service=ghcr.io&scope=repository:sdr3078/batzbakbot:pull",
    headers={"Authorization": "Basic " + basic})
tok = json.load(urllib.request.urlopen(req, timeout=20))["token"]
req = urllib.request.Request(
    "https://ghcr.io/v2/sdr3078/batzbakbot/manifests/latest", method="HEAD",
    headers={"Authorization": "Bearer " + tok,
             "Accept": "application/vnd.oci.image.index.v1+json,"
                       "application/vnd.docker.distribution.manifest.list.v2+json"})
if urllib.request.urlopen(req, timeout=20).status != 200:
    sys.exit("cannot pull the image with this credential")
# Same secret, new namespace. Server-populated metadata is dropped so ArgoCD
# does not try to reconcile a uid/resourceVersion from another object.
out = {"apiVersion": "v1", "kind": "Secret", "type": src["type"],
       "metadata": {"name": "ghcr-pull", "namespace": "'"$APP_NS"'"},
       "data": src["data"]}
json.dump(out, open("/tmp/ghcr-copy.json", "w"))
' || { echo "ERROR: the existing credential cannot pull this image. Nothing sealed." >&2; exit 1; }
  echo "      OK — pulled the manifest."
  seal < /tmp/ghcr-copy.json > "$GHCR_SEALED"
  shred -u /tmp/ghcr-copy.json 2>/dev/null || rm -f /tmp/ghcr-copy.json
  echo "      -> $GHCR_SEALED"
else
echo "      verifying it can actually pull this image ..."
ghcr_tok="$(curl -s -u "$GH_USER:$GHCR_PAT" \
  'https://ghcr.io/token?service=ghcr.io&scope=repository:sdr3078/batzbakbot:pull' \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
ghcr_code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${ghcr_tok:-none}" \
  -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
  https://ghcr.io/v2/sdr3078/batzbakbot/manifests/latest || echo 000)"
if [[ "$ghcr_code" != "200" ]]; then
  echo "ERROR: this token cannot pull ghcr.io/sdr3078/batzbakbot (HTTP $ghcr_code)." >&2
  echo "       Needs a CLASSIC token with read:packages. A wrong one here does not fail" >&2
  echo "       until the pod ImagePullBackOffs on the cluster. Nothing sealed." >&2
  exit 1
fi
echo "      OK — pulled the manifest (HTTP 200)."
unset ghcr_tok ghcr_code
kubectl create secret docker-registry ghcr-pull -n "$APP_NS" \
  --docker-server=ghcr.io --docker-username="$GH_USER" \
  --docker-password="$GHCR_PAT" --docker-email="$GH_EMAIL" \
  --dry-run=client -o yaml \
| seal > "$GHCR_SEALED"
echo "      -> $GHCR_SEALED"
fi

# === 3/5 · the real config.yaml ==============================================
# Read from the file rather than a prompt — it is multi-line YAML — but never
# copied anywhere: --from-file streams it straight into the sealing pipe.
CONFIG_SRC="${BATZBAKBOT_CONFIG:-$BOT_DIR/config.yaml}"
echo "[3/5] config.yaml from: $CONFIG_SRC"
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

# === 4/5 · proxy credentials + admin phone ===================================
# PROXY_CHECK carries the poll traffic and SHOULD rotate freely. PROXY_BUY
# carries a purchase and MUST be sticky: one order spans an overview load, a
# reservation and a form submit bound to a single PHPSESSID, so an exit that
# rotates mid-flow breaks the session after a real ticket is already held.
printf '[4/5] PROXY_CHECK (poll egress, scheme://user:pass@host:port; empty = direct): '
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

# === 5/5 · basic-auth for the whatsapp Ingress ===============================
# The Ingress publishes an API with NO authentication of its own onto a
# LAN-resolvable host, so nginx must gate it. htpasswd format, APR1 hashed by
# openssl -- the password is never stored, only its hash, and the hash is what
# gets sealed.
printf '[5/5] username for https://whatsapp.lab.batzbak.top: '
read -r WA_USER
[[ -n "${WA_USER:-}" ]] || { echo "ERROR: empty username." >&2; exit 1; }
printf '      password (not echoed): '; read -rs WA_PASS; echo
printf '      repeat it: ';            read -rs WA_PASS2; echo
[[ -n "${WA_PASS:-}" ]] || { echo "ERROR: empty password." >&2; exit 1; }
[[ "$WA_PASS" == "$WA_PASS2" ]] || { echo "ERROR: passwords differ. Nothing sealed." >&2; exit 1; }
# A weak one here is the whole control: this host is reachable from every
# address on the /22.
[[ ${#WA_PASS} -ge 12 ]] || { echo "ERROR: use at least 12 characters -- this is the only thing between the LAN and full control of the paired WhatsApp account." >&2; exit 1; }
WA_HASH="$(openssl passwd -apr1 "$WA_PASS")"
kubectl create secret generic whatsapp-basic-auth -n "$APP_NS" \
  --from-literal=auth="${WA_USER}:${WA_HASH}" \
  --dry-run=client -o yaml \
| seal > "$AUTH_SEALED"
unset WA_PASS WA_PASS2 WA_HASH
echo "      -> $AUTH_SEALED"

# --- sanity: each sealed file carries the expected encrypted keys -------------
grep -q 'password'         "$REPO_SEALED"   || { echo "ERROR: repo seal incomplete." >&2; exit 1; }
grep -q 'dockerconfigjson' "$GHCR_SEALED"   || { echo "ERROR: ghcr seal incomplete." >&2; exit 1; }
grep -q 'config.yaml'      "$CONFIG_SEALED" || { echo "ERROR: config seal incomplete." >&2; exit 1; }
for k in PROXY_CHECK PROXY_BUY ADMIN_PHONE; do
  grep -q "$k" "$APP_SEALED" || { echo "ERROR: $k missing from $APP_SEALED." >&2; exit 1; }
done
grep -q 'auth' "$AUTH_SEALED" || { echo "ERROR: basic-auth seal incomplete." >&2; exit 1; }
# Nothing plaintext must ever survive in these files.
if grep -RqE '^[[:space:]]*(stringData|data):' "$REPO_SEALED" "$GHCR_SEALED" "$CONFIG_SEALED" "$APP_SEALED" "$AUTH_SEALED"; then
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
