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

# ENTER keeps whatever is already sealed. This script has to be re-runnable to
# add ONE new credential without re-entering the others, and some of them cannot
# be re-entered at all: GitHub displays a fine-grained PAT exactly once, so
# demanding it again would mean revoking a working token purely to add an
# unrelated secret. A step that is skipped leaves its existing file untouched.
keep_or_die() {           # keep_or_die <sealed-file> <what-it-is>
  if [[ -s "$1" ]]; then
    echo "      nothing entered — keeping the existing seal: $1"
    return 0
  fi
  echo "ERROR: nothing entered for $2, and there is no existing $1 to keep." >&2
  echo "       This one has no fallback: it must be provided at least once." >&2
  return 1
}

# For steps that collect SEVERAL values, a bare ENTER is ambiguous: an empty
# PROXY_CHECK is itself a valid setting ("poll direct from this host"), so it
# cannot double as "keep what is there". Those ask outright. -> 0 means collect.
replace_existing() {      # replace_existing <sealed-file>
  [[ -s "$1" ]] || return 0
  local reply
  printf '      (already sealed) ENTER keeps it, or type "new" to replace: '
  read -r reply
  [[ "$reply" == "new" ]]
}

# === 1/5 · ArgoCD repo-clone credential (fine-grained PAT, Contents: Read) ====
echo "[1/5] ArgoCD repo-clone credential."
[[ -s "$REPO_SEALED" ]] && echo "      (already sealed — press ENTER to keep it)"
printf '      GitHub token (fine-grained, Contents:Read on batzbakbot), or ENTER: '
read -rs REPO_PAT; echo
if [[ -z "${REPO_PAT:-}" ]]; then
  keep_or_die "$REPO_SEALED" "the ArgoCD repo credential" || exit 1
else
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
fi

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
if [[ -s "$GHCR_SEALED" ]]; then
  echo "      (already sealed — press ENTER to keep it)"
else
  echo "      Press ENTER with no input to COPY the existing ghcr-pull from another"
  echo "      namespace (${GHCR_SOURCE_NS:-wardstone}) instead of retyping the token."
fi
printf '      GHCR token (classic, read:packages), or ENTER: '
read -rs GHCR_PAT; echo

if [[ -z "${GHCR_PAT:-}" && -s "$GHCR_SEALED" ]]; then
  echo "      nothing entered — keeping the existing seal: $GHCR_SEALED"
elif [[ -z "${GHCR_PAT:-}" ]]; then
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
# Asks like the others even though it needs no secret typed, because re-sealing
# is not free of consequence in a Git repo: sealed-secrets draws a fresh random
# session key every time, so identical plaintext produces different ciphertext
# and every run would rewrite this file with a diff that means nothing. Say
# "new" when config.yaml has actually changed.
if ! replace_existing "$CONFIG_SEALED"; then
  echo "      keeping the existing seal: $CONFIG_SEALED"
else
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
fi

# === 4/5 · proxy credentials + admin phone ===================================
# PROXY_CHECK carries the poll traffic and SHOULD rotate freely. PROXY_BUY
# carries a purchase and MUST be sticky: one order spans an overview load, a
# reservation and a form submit bound to a single PHPSESSID, so an exit that
# rotates mid-flow breaks the session after a real ticket is already held.
echo "[4/5] proxy egresses + admin phone."
if ! replace_existing "$APP_SEALED"; then
  echo "      keeping the existing seal: $APP_SEALED"
else
printf '      PROXY_CHECK (poll egress, scheme://user:pass@host:port; empty = direct): '
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
fi

# === 5/5 · basic-auth for the whatsapp Ingress ===============================
# The Ingress publishes an API with NO authentication of its own onto a
# LAN-resolvable host, so nginx must gate it. htpasswd format, APR1 hashed by
# openssl -- the password is never stored, only its hash, and the hash is what
# gets sealed.
echo "[5/5] basic-auth for https://whatsapp.lab.batzbak.top"
if ! replace_existing "$AUTH_SEALED"; then
  echo "      keeping the existing seal: $AUTH_SEALED"
else
printf '      username: '
read -r WA_USER
[[ -n "${WA_USER:-}" ]] || { echo "ERROR: empty username." >&2; exit 1; }
printf '      password (not echoed): '; read -rs WA_PASS; echo
printf '      repeat it: ';            read -rs WA_PASS2; echo
[[ -n "${WA_PASS:-}" ]] || { echo "ERROR: empty password." >&2; exit 1; }
[[ "$WA_PASS" == "$WA_PASS2" ]] || { echo "ERROR: passwords differ. Nothing sealed." >&2; exit 1; }
# Floor is 8, set deliberately rather than by default. This string is the only
# thing between the LAN and full control of the paired WhatsApp account, and the
# host is reachable from every address on the /22 -- but the exposure is
# LAN-only (lab.batzbak.top is NXDOMAIN publicly), so this is a considered
# trade-off, not an oversight. Raise it here if that ever stops being true.
[[ ${#WA_PASS} -ge 8 ]] || { echo "ERROR: use at least 8 characters." >&2; exit 1; }
WA_HASH="$(openssl passwd -apr1 "$WA_PASS")"
kubectl create secret generic whatsapp-basic-auth -n "$APP_NS" \
  --from-literal=auth="${WA_USER}:${WA_HASH}" \
  --dry-run=client -o yaml \
| seal > "$AUTH_SEALED"
unset WA_PASS WA_PASS2 WA_HASH
echo "      -> $AUTH_SEALED"
fi

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

echo
echo "Sealed. NEXT STEPS"
echo
echo "  1. Review the changed files (git status), then commit + push. They are"
echo "     encrypted to THIS cluster's controller key and are useless elsewhere."
echo

# The rest is state-dependent. Printing a fixed first-run checklist on every run
# is worse than printing nothing: it tells you to do things already done, and on
# a re-run every one of these was already true -- which trains you to skip the
# list entirely, including the step that still mattered.
if kubectl -n argocd get appproject batzbakbot >/dev/null 2>&1; then
  echo "  2. AppProject 'batzbakbot' is already applied. Nothing to do."
else
  echo "  2. Push charts/argocd/batzbakbot-project.yaml FIRST (or in the same push):"
  echo "     both Applications name project: batzbakbot, and a sync against a project"
  echo "     that does not exist yet is rejected."
fi
echo

bot_tag="$(sed -n 's/^ *newTag: *//p' "$BOT_DIR/k8s/kustomization.yaml" 2>/dev/null | head -1)"
if [[ -n "$bot_tag" && "$bot_tag" != "bootstrap" ]]; then
  echo "  3. Image is built and pinned: $bot_tag. Nothing to do."
else
  echo "  3. Push the app repo so CI builds an image and bumps k8s/kustomization.yaml."
  echo "     Until then the tag is 'bootstrap', which nothing publishes, and the pod"
  echo "     ImagePullBackOffs on purpose rather than deploying something unintended."
fi
echo

if kubectl -n batzbakbot get ingress whatsapp >/dev/null 2>&1; then
  echo "  4. Pair WhatsApp at https://whatsapp.lab.batzbak.top (nginx basic auth)"
  echo "     BEFORE turning AUTO_PURCHASE on. Register EXACTLY ONE device -- the"
  echo "     server defaults to it, which is why the bot sends no X-Device-Id; a"
  echo "     second device makes every notification 400."
else
  echo "  4. Pair WhatsApp before turning AUTO_PURCHASE on. No Ingress yet, so:"
  echo "       kubectl -n batzbakbot port-forward svc/whatsapp 3000:3000"
  echo "     then open http://localhost:3000. Register EXACTLY ONE device."
fi
echo

# A changed config Secret only reaches the bot if something restarts it: the
# process reads config.yaml once, at startup.
if kubectl -n batzbakbot get deploy batzbakbot >/dev/null 2>&1; then
  echo "  5. Sync 'batzbakbot-bootstrap' in ArgoCD to publish the new Secret."
  echo "     Reloader then restarts the bot -- required, because it reads"
  echo "     config.yaml once at startup and never re-reads it."
else
  echo "  5. The workload is not deployed yet. Sync 'batzbakbot-bootstrap' first,"
  echo "     then 'batzbakbot' when you are ready for it to start polling."
fi
