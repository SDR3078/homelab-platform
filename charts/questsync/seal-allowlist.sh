#!/usr/bin/env bash
#
# seal-allowlist.sh — seal the QuestSync access ALLOWLIST (Habitica User IDs) into
# charts/questsync/allowlist-sealed.yaml and commit it to GitOps.
#
# The User IDs are identifiers, not credentials — but this repo is PUBLIC, so the
# ROSTER (who may use the bridge) travels as a SealedSecret, not a plaintext ConfigMap.
# The controller decrypts it to a Secret the pod mounts at /etc/questsync/allowlist/users
# and re-reads live (~30s after the mount updates) — no restart to change the list.
#
#   Usage:
#     ./seal-allowlist.sh <id1> <id2> ...      # IDs as arguments
#     ./seal-allowlist.sh < roster.txt         # one ID per line (file/stdin)
#
# Provide the FULL desired roster each run (replace semantics). See the current one:
#   kubectl get secret questsync-allowlist -n questsync -o jsonpath='{.data.users}' | base64 -d
#
set -euo pipefail

REPO_DIR="${HOMELAB_DIR:-$HOME/homelab-platform}"
SEALED_FILE="charts/questsync/allowlist-sealed.yaml"
SECRET_NAME="questsync-allowlist"
NAMESPACE="questsync"
CONTROLLER_NAME="sealed-secrets-controller"
CONTROLLER_NS="sealed-secrets"

cd "$REPO_DIR"
for b in kubectl kubeseal git; do
  command -v "$b" >/dev/null || { echo "ERROR: '$b' not found in PATH." >&2; exit 1; }
done

# Gather IDs from args (if any) or stdin; strip '#' comments, split on comma/space/newline,
# drop blanks, dedupe.
ids="$(mktemp)"; trap 'rm -f "$ids"' EXIT
{ [ "$#" -gt 0 ] && printf '%s\n' "$@" || cat; } \
  | sed 's/#.*//' | tr ', \t' '\n' | sed '/^$/d' | sort -u > "$ids"

n="$(wc -l < "$ids" | tr -d ' ')"
[ "$n" -ge 1 ] || { echo "ERROR: no User IDs given — refusing to seal an EMPTY allowlist (it would deny EVERYONE)." >&2; exit 1; }

# Habitica User IDs are UUIDs — warn (don't block) on anything not UUID-shaped, to catch typos.
while read -r id; do
  [[ "$id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || echo "WARNING: '$id' isn't UUID-shaped — is that really a Habitica User ID?" >&2
done < "$ids"

echo "Sealing $n allowed Habitica User ID(s):"; sed 's/^/  - /' "$ids"

kubectl create secret generic "$SECRET_NAME" -n "$NAMESPACE" \
  --from-file=users="$ids" --dry-run=client -o yaml \
| kubeseal --controller-name="$CONTROLLER_NAME" --controller-namespace="$CONTROLLER_NS" -o yaml \
  > "$SEALED_FILE"
grep -q 'kind: SealedSecret' "$SEALED_FILE" || { echo "ERROR: seal failed (no SealedSecret produced)." >&2; exit 1; }
echo "  -> wrote $SEALED_FILE"

git add "$SEALED_FILE"
if git diff --cached --quiet; then echo "No change (roster identical to what's committed)."; exit 0; fi
git commit -m "feat(questsync): access allowlist — ${n} user(s)"
branch="$(git branch --show-current)"
remote="$(git remote -v | awk '$3=="(push)" && /homelab-platform/ {print $1; exit}')"; remote="${remote:-origin}"
GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash "$remote" "$branch" >/dev/null 2>&1 || true
GIT_TERMINAL_PROMPT=0 git push "$remote" "$branch"

cat <<'NOTE'

Done. ArgoCD -> sealed-secrets controller decrypts -> the pod re-reads
/etc/questsync/allowlist/users. Allow a few minutes end-to-end, or force it now:
    argocd app sync questsync-bootstrap
NOTE
