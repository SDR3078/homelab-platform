# Cluster Rebuild — Disaster Recovery Runbook

End-to-end procedure to rebuild the entire homelab platform from scratch.
Written to be followed top-to-bottom on a fresh Proxmox host with nothing
deployed.

## Prerequisites

You will need:

- A Proxmox VE host with capacity for an 8GB / 80GB VM
- The sealed-secrets master-key backup file (`sealed-secrets-master-key-BACKUP.yaml`).
  IRREPLACEABLE — kept outside Git. If lost, see [Lost master key](#lost-master-key).
- Password manager entries for:
  - `homelab-platform/postgres-{mlflow,dagster,lakekeeper}`
  - `homelab-platform/minio-root`
  - `homelab-platform/postgres-backup-svcacct`
  - `homelab-platform/cloudflare-api-token` (only needed for rotation or
    master-key-loss recovery — the token itself is sealed in Git)
  - `homelab-platform/postgres-tenants-wedding` (the wedding-site CNPG role password)
  - `homelab-platform/wedding-site-auth-secret` (Auth.js JWT key — only
    needed for master-key-loss recovery; sealed in Git)
  - `homelab-platform/ghcr-pull-pat` (only needed for rotation when
    expiry hits — the token itself is sealed in Git)
  - `homelab-platform/argocd-wedding-site-repo-pat` (ArgoCD's clone
    credential for the private wedding-site repo; only needed for
    rotation — the token itself is sealed in Git)
- DNS for `*.lab.batzbak.top` (or your domain) resolvable from clients
- Devbox with `kubectl`, `helm`, `argocd`, `kubeseal`, `mc` CLIs

## Order of operations

### 1. Provision the k3s VM

Create a Debian 12 minimal VM on Proxmox:
- 8GB RAM, 80GB thin-provisioned disk, qemu-guest-agent
- VirtIO devices, Discard + SSD emulation enabled
- SeaBIOS (no UEFI, no TPM)

### 2. Install k3s on the VM

```bash
curl -sfL https://get.k3s.io | sh -s - \
  --disable=traefik --write-kubeconfig-mode=644
```

Traefik is disabled because ingress-nginx replaces it (and would conflict).
The mode flag makes the kubeconfig readable for SCP to the devbox.

### 3. Copy kubeconfig to devbox

```bash
scp k3s-01:/etc/rancher/k3s/k3s.yaml ~/.kube/config-homelab
# Edit ~/.kube/config-homelab — replace 127.0.0.1 with the VM's LAN IP
export KUBECONFIG=~/.kube/config-homelab
kubectl get nodes  # should show k3s-01 Ready
```

### 4. Restore the sealed-secrets master key (CRITICAL ORDER)

This MUST happen BEFORE the sealed-secrets controller ever starts. Apply the
backup directly:

```bash
kubectl create namespace sealed-secrets
kubectl apply -f /path/to/sealed-secrets-master-key-BACKUP.yaml

# Verify the restored key is in place
kubectl get secret -n sealed-secrets \
  -l sealedsecrets.bitnami.com/sealed-secrets-key
```

If you skip this step, the controller auto-generates a fresh keypair on first
start. Every committed sealed secret in this repo is encrypted to the OLD
public key and will fail to decrypt forever.

### 5. Install ArgoCD and apply the root Application

Follow `bootstrap/argocd-install.md` end-to-end (Steps 1-4). The final
step there applies `bootstrap/root-app.yaml`, after which ArgoCD takes
over.

Within 5-10 minutes ArgoCD will:
- Reconcile every Application in `apps/`
- sealed-secrets controller decrypts every `*-sealed.yaml` using the
  restored master key (postgres role passwords, MinIO root creds,
  Cloudflare API token, postgres-backup-credentials — all materialize
  automatically with no manual step)
- cert-manager, ingress-nginx, reflector come up
- CNPG operator + MinIO operator install
- data-platform Application syncs:
  - Postgres Cluster (postgres-data-platform) + Databases for first-party
    services (mlflow / dagster / lakekeeper)
  - MinIO Tenant + 4 buckets
  - Real Secrets materialize from sealed counterparts
- application-data Application syncs (tenant data tier):
  - Postgres Cluster (postgres-tenants) + Databases for tenant apps
    (wedding, etc.)
  - Per-tenant role credentials reflected into each tenant's namespace

Expected transient state: Postgres backups will FAIL in this window
because `postgres-backup-credentials` authenticates against an svcacct
that doesn't exist on the fresh MinIO Tenant. Step 7 below fixes this.
Until then, barman errors in the Postgres pod logs are expected and
not a defect.

### 6. Wait for the data plane to be Ready

```bash
kubectl get application -n argocd
kubectl get pods -n data-platform
kubectl get pods -n application-data
```

All Applications `Synced/Healthy`. Both Postgres Clusters
(postgres-data-platform, postgres-tenants) and the MinIO Tenant Running.

**Known race on first sync:** if `postgres-tenants` shows
`ContinuousArchiving=False` (`failed to get envs: cache miss`), the pod
started before reflector mirrored `postgres-backup-credentials` +
`postgres-backup-ca` into `application-data`. Fix:

```bash
kubectl delete pod postgres-tenants-1 -n application-data
```

The replacement pod finds the now-reflected secrets and populates its
config cache cleanly. Full procedure (including manual backup trigger
to flush the stale failed Backup CR) is in
`charts/application-data/postgres-tenants-cluster.yaml`'s runbook header
under "first-sync race with reflector".

### 7. Recreate the MinIO postgres-backup service account

This is a chicken-and-egg unique to MinIO: the sealed credentials in Git
authenticate against a service account that exists only in MinIO's internal
IAM database, not in K8s. A fresh Tenant has an empty IAM DB, so the
svcacct must be recreated.

Procedure: identical to credential rotation, documented inline in
`charts/data-platform/postgres-cluster.yaml`'s "backup credential rotation"
runbook. The policy JSON is committed at `bootstrap/postgres-backup-policy.json`
to skip the step of redrawing it.

Quick command summary:

```bash
# Port-forward to the live MinIO API
kubectl -n data-platform port-forward svc/minio 9000:443

# In another terminal, set up mc and create the policy + svcacct
read -s -p "MinIO root password: " MINIO_ROOT_PASSWORD; echo
mc alias set local https://localhost:9000 admin "$MINIO_ROOT_PASSWORD" --insecure
mc admin policy create --insecure local postgres-backup-policy bootstrap/postgres-backup-policy.json
mc admin user svcacct add --insecure local admin --policy bootstrap/postgres-backup-policy.json
# Save the printed Access Key + Secret Key.
```

After capturing the keys, reseal `postgres-backup-credentials` and commit:

```bash
kubectl create secret generic postgres-backup-credentials \
  --namespace data-platform \
  --from-literal=ACCESS_KEY_ID="<NEW_ACCESS_KEY>" \
  --from-literal=ACCESS_SECRET_KEY="<NEW_SECRET_KEY>" \
  --dry-run=client -o yaml > /tmp/postgres-backup-credentials.yaml
kubeseal -f /tmp/postgres-backup-credentials.yaml \
  -w charts/data-platform/postgres-backup-credentials-sealed.yaml \
  --controller-name=sealed-secrets-controller \
  --controller-namespace=sealed-secrets
shred -u /tmp/postgres-backup-credentials.yaml

git add charts/data-platform/postgres-backup-credentials-sealed.yaml
git commit -m "reseal postgres-backup-credentials for new MinIO Tenant"
git push
```

ArgoCD syncs the new sealed Secret → controller decrypts → CNPG starts
using working keys.

### 8. Verify backups end-to-end

```bash
kubectl get backups.postgresql.cnpg.io -n data-platform -w
```

The `immediate: true` flag on `postgres-data-platform-weekly` triggers a
base backup right after the ScheduledBackup applies. Expect a Backup CR
to appear and transition to `phase: completed` within a few minutes.

### 9. Re-confirm the master-key backup matches in-cluster state

```bash
kubectl get secret -n sealed-secrets \
  -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
  > /tmp/refreshed-backup.yaml
diff /tmp/refreshed-backup.yaml /path/to/sealed-secrets-master-key-BACKUP.yaml
```

Should be identical apart from `generation` / `resourceVersion` fields.

### 10. Tenant apps

The platform hosts tenant apps whose chart lives in a separate repository.
Each tenant app gets two ArgoCD Applications:
- `<app>-bootstrap` — in-repo glue (namespace + sealed secrets), sync-wave 0
- `<app>` — the chart itself, pointing at the tenant repo's `helm/` path,
  sync-wave 1

Currently deployed tenant apps:
- `wedding-site` (https://github.com/SDR3078/wedding-site)

Per-tenant rebuild shape (after Step 9 finishes):

1. **Database tier comes up automatically.** The application-data
   Application materializes `postgres-tenants` with all per-tenant roles
   + Databases declared in `charts/application-data/postgres-tenants-cluster.yaml`'s
   `managed.roles` and Database CRDs. No imperative provisioning. The
   sealed credentials in `charts/application-data/<tenant>-credentials-sealed.yaml`
   are decrypted by the controller and reflected into each tenant's
   namespace as `<tenant>-credentials`.

2. **Re-seal any non-DB tenant secrets** (Auth.js secret, GHCR PAT, etc.)
   if the master-key was lost. Each `*-sealed.yaml` in `charts/<app>/`
   has a full kubeseal procedure inline. Plaintext recovery inputs come
   from the password manager entries listed in Prerequisites. (DB
   passwords are sealed in `charts/application-data/`; reseal there if
   needed, NOT in the tenant chart directory.)

3. **First-time schema push.** Tenant chart Applications come up healthy
   only once the tenant DB has the expected schema. For a fresh rebuild,
   port-forward into `postgres-tenants-rw` on `application-data` and
   run the tenant repo's `npm run db:push` (or migration equivalent)
   once per tenant:

   ```bash
   kubectl port-forward -n application-data svc/postgres-tenants-rw 5433:5432 &
   # password from password manager: 'homelab-platform/postgres-tenants-<tenant>'
   cd /path/to/<tenant>
   DATABASE_URL="postgresql://<tenant>:<pw>@localhost:5433/<tenant>" \
     npm run db:push
   ```

4. **Tenant chart Applications already in `apps/`** reconcile
   automatically — bootstrap (wave 0) creates the namespace + reseals,
   chart Application (wave 1) applies the Deployment. Brief
   `CreateContainerConfigError` on first pod schedule is expected (~30s)
   and self-heals once the reflected `<tenant>-credentials` Secret
   propagates.

Tenant-side generic deploy guide (placeholders that any deployer fills
in for their cluster — the homelab-platform-specific values live in
`charts/<app>/` here):
- wedding-site: https://github.com/SDR3078/wedding-site/blob/main/deploy/README.md

## Lost master key

If the master-key backup is unavailable, sealed secrets in Git become
permanently undecryptable. Recovery:

1. Skip Step 4. Let the controller generate a fresh keypair on first start.
2. For every committed `*-sealed.yaml`:
   - Retrieve the plaintext value(s) from the password manager
   - Re-run the file's kubeseal procedure (each sealed YAML has the
     procedure inline as an OPERATIONS RUNBOOK)
3. Commit the re-sealed files, push.
4. Back up the new master key immediately.

Lost passwords (not in password manager) are NOT recoverable — operational
hygiene around the password manager is the only defense.

## Restoring data from backup

Cluster rebuild ≠ data restoration. To restore Postgres data from the
MinIO-stored backups into a freshly-bootstrapped Cluster, point a new
Cluster's `spec.bootstrap.recovery` at the existing `barmanObjectStore`.
Reference: https://cloudnative-pg.io/documentation/current/recovery/
