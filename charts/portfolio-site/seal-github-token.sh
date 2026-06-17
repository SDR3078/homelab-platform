#!/usr/bin/env bash
#
# seal-github-token.sh — add (or rotate) the read-only GITHUB_TOKEN in the
# portfolio-site `groq-credentials` SealedSecret and commit it to GitOps.
#
# The token is read from a SILENT prompt — never passed as an argument, echoed,
# written to disk in plaintext, or stored in shell history. This script holds
# no secret and is safe to commit.
#
#   Usage:  ./seal-github-token.sh
#
set -euo pipefail

# --- config (matches the reseal recipe in groq-credentials-sealed.yaml) ------
REPO_DIR="${HOMELAB_DIR:-$HOME/homelab-platform}"
SEALED_FILE="charts/portfolio-site/groq-credentials-sealed.yaml"
SECRET_NAME="groq-credentials"
NAMESPACE="portfolio-site"
CONTROLLER_NAME="sealed-secrets-controller"
CONTROLLER_NS="sealed-secrets"
# -----------------------------------------------------------------------------

cd "$REPO_DIR"

for bin in kubectl kubeseal git curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not found in PATH." >&2; exit 1; }
done
[[ -f "$SEALED_FILE" ]] || { echo "ERROR: $SEALED_FILE not found under $REPO_DIR." >&2; exit 1; }

resp="$(mktemp)"
trap 'unset GH_TOKEN 2>/dev/null || true; rm -f "$resp"' EXIT

printf 'Paste the fine-grained GitHub token, then press Enter: '
read -rs GH_TOKEN
echo
[[ -n "${GH_TOKEN:-}" ]] || { echo "ERROR: no token entered." >&2; exit 1; }
case "$GH_TOKEN" in
  github_pat_*) : ;;
  ghp_*) echo "WARNING: that's a CLASSIC PAT (full-account). A fine-grained, public-read token is strongly preferred." >&2 ;;
  *)     echo "ERROR: that doesn't look like a GitHub token." >&2; exit 1 ;;
esac

echo "Validating the token against api.github.com ..."
http="$(curl -s -o "$resp" -w '%{http_code}' \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        https://api.github.com/rate_limit || echo 000)"
[[ "$http" == "200" ]] || { echo "ERROR: token did not authenticate (HTTP $http). Nothing sealed." >&2; exit 1; }
if command -v jq >/dev/null; then
  limit="$(jq -r '.resources.core.limit' "$resp" 2>/dev/null || echo '?')"
  echo "  OK — authenticated; core rate limit ${limit}/hr."
  [[ "$limit" == "5000" ]] || echo "  (note: expected 5000 — double-check the token's scope.)"
else
  echo "  OK — token authenticates."
fi

echo "Sealing GITHUB_TOKEN into $SEALED_FILE (GROQ_API_KEY left untouched) ..."
kubectl create secret generic "$SECRET_NAME" -n "$NAMESPACE" \
  --from-literal=GITHUB_TOKEN="$GH_TOKEN" --dry-run=client -o yaml \
| kubeseal --controller-name="$CONTROLLER_NAME" \
           --controller-namespace="$CONTROLLER_NS" \
           -o yaml --merge-into "$SEALED_FILE"

for k in GROQ_API_KEY GITHUB_TOKEN; do
  grep -q "$k" "$SEALED_FILE" || { echo "ERROR: $k missing from $SEALED_FILE after seal." >&2; exit 1; }
done
echo "  OK — both GROQ_API_KEY and GITHUB_TOKEN present (encrypted)."

echo "Committing to GitOps ..."
git add "$SEALED_FILE"
if git diff --cached --quiet; then
  echo "  Nothing changed (token identical to what's already sealed). Done."
  exit 0
fi
git commit -m "feat(portfolio-site): add read-only GITHUB_TOKEN to groq-credentials"
branch="$(git branch --show-current)"
GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash origin "$branch" >/dev/null 2>&1 || true
GIT_TERMINAL_PROMPT=0 git push origin "$branch"

cat <<'NOTE'

Done. ArgoCD syncs the SealedSecret -> the controller decrypts it -> Stakater
Reloader rolls the portfolio-site-api pod. The chat agent's GitHub rate limit
goes 60 -> 5000 req/hr once the pod restarts (usually a minute or two).
NOTE
