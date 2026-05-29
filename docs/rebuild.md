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
  - `homelab-platform/postgres-{mlflow,airflow,lakekeeper}`
  - `homelab-platform/minio-root`
  - `homelab-platform/postgres-backup-svcacct`
  - `homelab-platform/cloudflare-api-token` (only needed for rotation or
    master-key-loss recovery — the token itself is sealed in Git)
  - `homelab-platform/postgres-tenants-wedding` (the wedding-site CNPG role password)
  - `homelab-platform/wedding-site-auth-secret` (Auth.js JWT key — only
    needed for master-key-loss recovery; sealed in Git)
  - `homelab-platform/wedding-site-api-token` (bearer token for the
    read-only RSVPs CSV API; consumed by the wedding planner via Excel
    Power Query — only needed for rotation or master-key-loss recovery;
    sealed in Git)
  - `homelab-platform/ghcr-pull-pat` (only needed for rotation when
    expiry hits — the token itself is sealed in Git)
  - `homelab-platform/argocd-wedding-site-repo-pat` (ArgoCD's clone
    credential for the private wedding-site repo; only needed for
    rotation — the token itself is sealed in Git)
  - `homelab-platform/minio-mlflow-artifacts-svcacct` (MinIO svcacct
    keys scoped to the mlflow-artifacts bucket; only needed for
    rotation or master-key-loss recovery — sealed in Git)
  - `homelab-platform/airflow-admin` (Airflow web UI admin password;
    used by `/tmp/setup-airflow-admin.sh` after every rebuild because
    the ab_user table is recreated empty)
  - `homelab-platform/lakekeeper-encryption-key` (at-rest encryption
    key for Lakekeeper's catalog-stored secrets; NEVER rotates —
    losing the key invalidates all catalog-stored warehouse credentials
    forever; only needed for master-key-loss recovery — sealed in Git)
  - `homelab-platform/minio-iceberg-warehouse-svcacct` (MinIO svcacct
    keys scoped to the iceberg-warehouse bucket; only needed for
    rotation or master-key-loss recovery — sealed in Git, but ALSO
    required as the body of warehouse-create API requests post-rebuild)
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
- cert-manager, ingress-nginx, reflector, reloader come up
  (reloader rolls workloads on Secret/ConfigMap change — tenant
  Deployments opt in via `secret.reloader.stakater.com/reload`
  annotation; see `apps/reloader.yaml` for the full pattern)
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
- Platform service Applications sync:
  - mlflow-bootstrap (wave 0) + mlflow (wave 1) — mlflow tracking
    server + model registry (artifact write fails until Step 7b)
  - airflow-bootstrap (wave 0) + airflow (wave 1) — Airflow 3.x
    orchestrator (api-server + scheduler + triggerer + dagProcessor +
    cleanup CronJob). Migration Job runs automatically and populates
    the schema; admin user creation is the manual Step 10.
  - lakekeeper-bootstrap (wave 0) + lakekeeper (wave 1) — Lakekeeper
    Iceberg REST Catalog. Migration Job runs automatically. Once
    healthy, the catalog needs to be BOOTSTRAPPED via POST
    /management/v1/bootstrap and warehouses CREATED via POST
    /management/v1/warehouse (one-time per cluster lifecycle, see the
    runbook header in charts/lakekeeper/namespace.yaml).

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

### 7. Recreate MinIO service accounts for platform consumers

This is a chicken-and-egg unique to MinIO: sealed credentials in Git
authenticate against service accounts that exist only in MinIO's internal
IAM database, not in K8s. A fresh Tenant has an empty IAM DB, so each
svcacct must be recreated. Two are needed at this point:

| Svcacct | Policy file | Sealed creds destination |
|---|---|---|
| postgres-backup | `bootstrap/postgres-backup-policy.json` | `charts/data-platform/postgres-backup-credentials-sealed.yaml` |
| mlflow-artifacts | `bootstrap/mlflow-artifacts-policy.json` | `charts/mlflow/mlflow-s3-credentials-sealed.yaml` |
| iceberg-warehouse | `bootstrap/iceberg-warehouse-policy.json` | `charts/lakekeeper/lakekeeper-s3-credentials-sealed.yaml` |

#### 7a. postgres-backup svcacct

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

#### 7b. mlflow-artifacts svcacct

Same shape as 7a, but scripted end-to-end (mc + kubeseal in one shot)
because the mlflow ns and SealedSecret destination are created by
`apps/mlflow-bootstrap.yaml`, which converges to a real Secret only
once this svcacct is materialized in MinIO and resealed.

Regenerate the setup script (preserved verbatim across sessions; see
session 6's note on the imperative-MinIO pattern):

```bash
# Run from the homelab-platform repo root. The script:
#   1. Prompts for MinIO root password
#   2. Port-forwards to the live MinIO API
#   3. Creates the named policy (mlflow-artifacts-policy) from
#      bootstrap/mlflow-artifacts-policy.json
#   4. Creates a service account under 'admin' attached to that policy
#   5. Seals AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY into
#      charts/mlflow/mlflow-s3-credentials-sealed.yaml
#   6. Prints both keys in a banner — save to password manager entry
#      "homelab-platform/minio-mlflow-artifacts-svcacct"
/tmp/setup-mlflow-s3-svcacct.sh
```

If `/tmp/setup-mlflow-s3-svcacct.sh` is missing, regenerate it inline —
the policy is committed; the script content lives in this section's git
history via the commit that introduced it. The interactive seal+commit
flow is identical to postgres-backup-credentials above; rotation also
re-uses this script (it overwrites the sealed file in place).

After resealing, commit:

```bash
git add charts/mlflow/mlflow-s3-credentials-sealed.yaml
git commit -m "reseal mlflow-s3-credentials for new MinIO Tenant"
git push
```

ArgoCD syncs → controller decrypts → mlflow Deployment's
`artifactRoot.s3.existingSecret` reference resolves and the pod boots
its first artifact connection successfully.

#### 7c. iceberg-warehouse svcacct + Lakekeeper encryption key

Same scripted shape as 7b. The script also generates the Lakekeeper
encryption key (used for at-rest encryption of catalog-stored
credentials in Postgres). The encryption key NEVER rotates — losing
it means losing access to all catalog-stored warehouse credentials.

```bash
/tmp/setup-lakekeeper-secrets.sh
```

The script:
1. Generates a 32-byte random encryption key (base64 urlsafe)
2. Prompts for MinIO root password
3. Creates the named policy from `bootstrap/iceberg-warehouse-policy.json`
4. Creates a service account scoped to the iceberg-warehouse bucket
5. Seals BOTH into `charts/lakekeeper/`:
   - `lakekeeper-encryption-key-sealed.yaml` (key: `encryptionKey`)
   - `lakekeeper-s3-credentials-sealed.yaml` (keys: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)
6. Prints both sets of plaintext for the password manager:
   - `homelab-platform/lakekeeper-encryption-key` (DO NOT rotate)
   - `homelab-platform/minio-iceberg-warehouse-svcacct` (can rotate; reseal + warehouse re-creation needed)

After resealing, commit:

```bash
git add charts/lakekeeper/lakekeeper-encryption-key-sealed.yaml \
        charts/lakekeeper/lakekeeper-s3-credentials-sealed.yaml
git commit -m "reseal lakekeeper secrets for new MinIO Tenant"
git push
```

ArgoCD syncs → controller decrypts → Lakekeeper catalog pod boots,
migrations run, the API is reachable. Warehouses then need to be
re-created via POST /management/v1/warehouse with the svcacct creds
embedded — see the per-tenant runbook in
`charts/lakekeeper/namespace.yaml`'s header.

**IMPORTANT — warehouse storage profile must use `remote-signing-enabled: false`.**
Lakekeeper's chart default (`flavor: minio`, remote-signing on) advertises
its S3V4RestSigner endpoint to clients, but every engine we've tested
(PyIceberg's FsspecFileIO, DuckDB-iceberg) either implements it
incompletely or ignores it — causing writes to fail with AccessDenied
or HTTP 403. The fix is per-warehouse storage profile config; chart
values are NOT involved.

Warehouse-create body that works (use this shape in
`/tmp/lakekeeper-validate.sh` or any future warehouse-create flow):
```json
{
  "warehouse-name": "demo",
  "project-id": "00000000-0000-0000-0000-000000000000",
  "storage-profile": {
    "type": "s3",
    "bucket": "iceberg-warehouse",
    "key-prefix": "warehouses/demo",
    "endpoint": "https://minio.data-platform.svc.cluster.local",
    "region": "us-east-1",
    "path-style-access": true,
    "flavor": "s3-compat",
    "sts-enabled": false,
    "remote-signing-enabled": false
  },
  "storage-credential": {
    "type": "s3",
    "credential-type": "access-key",
    "aws-access-key-id": "<svcacct key>",
    "aws-secret-access-key": "<svcacct secret>"
  },
  "delete-profile": {"type": "hard"}
}
```

With remote-signing off, clients are responsible for sending their own
S3 credentials. The platform's pattern: reflect
`charts/lakekeeper/lakekeeper-s3-credentials-sealed.yaml` into the
client namespace (e.g., airflow) + load as env vars `LAKE_S3_ACCESS_KEY_ID`
/ `LAKE_S3_SECRET_ACCESS_KEY`; clients pass those into PyIceberg's
RestCatalog as `s3.access-key-id` / `s3.secret-access-key` (see
`airflow/dags/iceberg_smoke.py` for the live example).

To patch an EXISTING warehouse whose storage profile was created with
remote-signing on (e.g., from a stale runbook), use
`POST /management/v1/warehouse/{id}/storage` with the full profile
above (Lakekeeper requires the credential block re-supplied on
storage-profile updates).

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

### 10. Airflow admin user creation

The Airflow chart's `createUserJob` is disabled in `apps/airflow.yaml`
because its args template hardcodes the admin password as a plaintext
value in the Job's args — no envFrom or secretKeyRef path. Instead the
admin user is created imperatively after the chart's pods are Ready
and the migration Job has populated the schema.

```bash
# Run after airflow-{api-server,scheduler,dag-processor,triggerer}
# are 1/1 or 2/2 Ready in the airflow ns.
/tmp/setup-airflow-admin.sh
```

The script prompts for the admin password (from password manager entry
`homelab-platform/airflow-admin`), looks up the scheduler pod, and
runs `airflow users create -r Admin -u admin -p <password>`. The user
is persisted in the `ab_user` table of postgres-data-platform/airflow.

On master-key-loss rebuild only: ALL five Airflow secrets need to be
regenerated FIRST via `/tmp/setup-airflow-secrets.sh`. The script
generates fresh DB password + Fernet key + api-secret-key + jwt-secret
+ admin password in one shot and overwrites the sealed files at:
- `charts/data-platform/airflow-credentials-sealed.yaml`
- `charts/airflow/airflow-metadata-connection-sealed.yaml`
- `charts/airflow/airflow-fernet-key-sealed.yaml`
- `charts/airflow/airflow-api-secret-sealed.yaml`
- `charts/airflow/airflow-jwt-secret-sealed.yaml`

DB password + admin password both go to the password manager
(`homelab-platform/postgres-airflow`, `homelab-platform/airflow-admin`).
**Fernet-key rotation requires re-encrypting all stored Connections** —
don't run this script for rotation purposes unless you mean to wipe
Connection records. Targeted single-secret resealing is the correct
rotation path; the all-in-one script is bootstrap/disaster-recovery only.

After the sealed files are re-committed and ArgoCD applies them, run
`/tmp/setup-airflow-admin.sh` to recreate the admin user (the ab_user
table is empty on a fresh airflow database).

### 11. Tenant apps

The platform hosts tenant apps whose chart lives in a separate repository.
Each tenant app gets two ArgoCD Applications:
- `<app>-bootstrap` — in-repo glue (namespace + sealed secrets), sync-wave 0
- `<app>` — the chart itself, pointing at the tenant repo's `helm/` path,
  sync-wave 1

Currently deployed tenant apps:
- `wedding-site` (https://github.com/SDR3078/wedding-site)

Per-tenant rebuild shape (after Step 10 finishes):

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

### 12. insurance-retention MLOps showcase (ML workload)

The first real ML workload on the platform: a 3-model decision bundle
(profit / covid / cost) that is trained, registered, gated-promoted, then
served. The chart and DAG live *in this repo*; the container image and ML
code live in the workload repo
(https://github.com/SDR3078/insurance-retention), whose own README
documents the build / train / serve / promote commands. Two
ArgoCD-reconciled sides come up automatically once their dependencies
(Steps 5, 7, 10) are healthy:

- **Serving.** `apps/insurance-retention.yaml` points at
  `charts/insurance-retention/` (namespace [PSA restricted],
  `s3-credentials-sealed.yaml`, a Deployment that loads
  `models:/insurance-retention-bundle@production`, a Service, and an Ingress
  at `insurance-retention.lab.batzbak.top`).
- **Training.** `airflow/dags/insurance_retention_train.py` is git-synced
  into Airflow (Step 10). A KubernetesPodOperator launches the (code-only) workload
  image in the `airflow` namespace to run `train.py --register`, which reads
  `insurance_retention.bronze.train` from the lakehouse at a pinned snapshot (NOT a
  baked dataset). It is `schedule = image_asset | bronze_asset` (data-aware, AssetAny
  = retrain on new code OR new data). Two resolver tasks set the pod env: `resolve_image`
  (`dag_run.conf['image']` else the `ir_target_image` Variable else `:latest` →
  `IR_IMAGE_REF`) and `resolve_data_snapshot` (`dag_run.conf['snapshot_id']` else the
  `ir_bronze_snapshot_id` Variable → `IR_DATA_SNAPSHOT_ID`). The pod also gets `LAKE_S3_*`
  from `lakekeeper-s3-credentials` (reflected into the `airflow` ns from `lakekeeper`,
  Step 7c) to read the Iceberg data files.
- **CT trigger.** `airflow/dags/insurance_retention_image_sensor.py` (cron
  `*/15`) resolves the digest of `insurance-retention:latest` from GHCR
  anonymously (public package, no token), and when it differs from the Airflow
  Variable `ir_last_trained_image` it records the digest in the `ir_target_image`
  Variable and **updates the `insurance_retention_image` Asset**, which Airflow
  uses to auto-trigger the training DAG (the image -> train edge shows in the
  Asset graph; no TriggerDagRunOperator). The watermark advances on detection
  (fire once per digest). So a new CI image drives an automatic, reproducible
  retrain; promotion stays gated. The **data** edge is symmetric:
  `insurance_retention_bronze_ingest` (see *Data plane (bronze) + first seed* below)
  updates the `insurance_retention_bronze` Asset when new training data lands, which
  also auto-triggers the training DAG — so CT fires on new code OR new data. `train.py`
  stamps `image_ref` + `code_sha` + `data_snapshot_id` into the bundle tags for the full
  lineage triangle (the Dockerfile bakes `IR_CODE_SHA` from CI's `github.sha`; the
  snapshot id comes from the bronze DAG). Both DAGs set
  `is_paused_upon_creation=False` so they are active on a fresh deploy (new DAGs
  are otherwise paused by default and would silently never fire).
- **Scoring (batch == the live `/select`).** Three more DAGs close the medallion's scoring half
  (all `is_paused_upon_creation=False`):
  - `insurance_retention_bronze_score_ingest` — lands `score.csv` (a MinIO landing Parquet) into
    Iceberg `insurance_retention.bronze.score` (overwrite + content-hash watermark
    `ir_score_content_sha` + a no-targets DQ gate [exactly 500 rows; targets ABSENT; `DOB` present,
    not `Birth_date`] + provenance), and **stamps a 0-indexed `candidate_id` from the file's physical
    row order** — `score.csv` has no natural id, and this row id is what lets the batch selection line
    up with serving's POST-position `/select`. Publishes `ir_score_snapshot_id` and emits the
    `insurance_retention_bronze_score` Asset.
  - `insurance_retention_scoring` — `schedule = insurance_retention_production | insurance_retention_bronze_score`
    (AssetAny). A KPO runs `python -m training.score --season <year> --top-k 200` (module form, so
    `score.py`'s repo-level `import bundle` resolves with `/app` on the path — a plain
    `python /app/training/score.py` would NOT). It reads `bronze.score@ir_score_snapshot_id`, loads the
    `@production` bundle via the SAME shared loader + scoring as the serving app, and writes per-season
    `insurance_retention.gold.selections` (per-partition overwrite on `season`; columns: predictions +
    `rank` + `selected` + lineage [`bundle_id`/`bundle_version`, `covid_threshold`,
    `code_sha`/`data_snapshot_id` from the bundle manifest, `score_snapshot_id`, `scored_at`]).
  - `insurance_retention_promote` — `schedule=None` (deliberately manual + gated). A KPO runs
    `promote_bundle.py` (gate `nv_uplift_cv ≥ 0.30`, atomic `@production` on all four models with
    rollback); on success a downstream `announce_promotion` task emits the `insurance_retention_production`
    Asset, which **auto-cascades a re-score** (the `production` edge → `scoring`). So the full causal
    chain is *data/code → retrain → gate → promote → score*. (A standalone script can't emit an Airflow
    Asset, which is why promotion is wrapped in a DAG.)

  **Promotion ≠ serving reload.** Serving loads `@production` once, at startup; a promotion is an alias
  move (not a new image), so ArgoCD does NOT roll the Deployment. After promoting, run
  `kubectl rollout restart deploy/insurance-retention -n insurance-retention` so serving loads the new
  bundle — only then does batch == the live `/select`.

**Data plane (bronze) + first seed.** Training reads
`insurance_retention.bronze.train`, so on a fresh lakehouse that table must be seeded
before the first retrain can register. `airflow/dags/insurance_retention_bronze_ingest.py`
reads a Parquet from a MinIO **landing zone** and writes the Iceberg table (overwrite +
content-hash fire-once watermark + DQ gate + provenance) via Lakekeeper, then emits the
`insurance_retention_bronze` Asset and publishes the snapshot id (`ir_bronze_snapshot_id`).
The landing object does not survive a MinIO rebuild, so re-seed it once from the example
data in the workload repo:

```bash
# From a clone of the insurance-retention workload repo:
.venv/bin/python -c "import pandas as pd; pd.read_excel('data/df_final.xlsx').to_parquet('/tmp/df_final.parquet')"

# Upload to the landing zone, using the iceberg-warehouse svcacct keys (Step 7c) and
# the same MinIO port-forward as Step 7. NOTE: the mc alias name MUST start with a letter
# -- an invalid name (e.g. a leading underscore) makes `mc cp` silently write to a LOCAL
# path instead of erroring, and `mc ls` will "confirm" that bogus local copy.
mc alias set lakeseed https://localhost:9000 "$IW_ACCESS_KEY" "$IW_SECRET_KEY" --insecure
mc cp --insecure /tmp/df_final.parquet \
  lakeseed/iceberg-warehouse/landing/insurance_retention/df_final.parquet
mc ls --insecure lakeseed/iceberg-warehouse/landing/insurance_retention/   # verify it landed in MinIO
```

`lakekeeper-s3-credentials` reflects into the `airflow` ns, so both the bronze-ingest
worker task and the training pod read the bucket with no extra secret to seal here.

**Scoring data plane (`bronze.score`).** The scoring DAG reads `insurance_retention.bronze.score`,
seeded the same way as `bronze.train` but from `score.csv` (the 2022 scoring cohort, no targets).
Row order is the contract — `candidate_id` is `range(len)` over the landed file — so do not reorder it:

```bash
# From a clone of the workload repo (same MinIO port-forward + iceberg-warehouse svcacct as above):
.venv/bin/python -c "import pandas as pd; pd.read_csv('data/score.csv').to_parquet('/tmp/score.parquet', index=False)"
mc cp --insecure /tmp/score.parquet \
  lakeseed/iceberg-warehouse/landing/insurance_retention/score.parquet
mc ls --insecure lakeseed/iceberg-warehouse/landing/insurance_retention/   # df_final.parquet + score.parquet
```

**Container image.** `ghcr.io/sdr3078/insurance-retention` is built and
pushed by the workload repo's CI (`.github/workflows/image.yaml`); the GHCR
*package* is public. Both the serving Deployment and the training KPO pull
it. On rebuild the image already exists on GHCR, so there is nothing to do
unless the package was deleted (re-run that repo's CI, or push a commit, to
republish).

**Sealed secrets (two copies of the `mlflow-artifacts` MinIO service account
from Step 7).** Both are named `insurance-retention-s3-credentials`,
resealed per namespace:
- `charts/insurance-retention/s3-credentials-sealed.yaml`
  (insurance-retention namespace): serving *downloads* model artifacts from
  MinIO.
- `charts/airflow/insurance-retention-s3-credentials-sealed.yaml` (airflow
  namespace): the training KPO pod *uploads* artifacts to MinIO.

Both decrypt with the restored master-key (Step 4), so there is **no
regeneration on a clean rebuild**. Reseal only if the `mlflow-artifacts`
service account is rotated, using the same read-live, build-manifest,
kubeseal procedure as Step 7 (target the correct namespace each time).

**Wildcard TLS.** `insurance-retention` is already listed in the reflection
allow / auto annotations in `charts/cert-manager/certificate-wildcard.yaml`
(Step 5), so `wildcard-lab-tls` reflects into the namespace and the Ingress
terminates TLS with the browser-trusted cert.

**First-boot ordering (the one non-obvious bit).** Serving loads
`@production`. On a fresh registry no bundle is promoted yet, so the pod
holds at `/health` 503 (the startupProbe keeps it un-ready instead of
crash-looping). And training now reads `bronze.train`, so the lakehouse must be
seeded before a candidate can be registered. Full sequence:

```bash
# 0. Seed the lakehouse (see the two seed blocks above): land df_final AND score.csv in the
#    MinIO landing zone, then create the bronze tables:
kubectl exec -n airflow deploy/airflow-scheduler -c scheduler -- \
  airflow dags trigger insurance_retention_bronze_ingest         # -> bronze.train + ir_bronze_snapshot_id
kubectl exec -n airflow deploy/airflow-scheduler -c scheduler -- \
  airflow dags trigger insurance_retention_bronze_score_ingest   # -> bronze.score + ir_score_snapshot_id

# 1. Register a candidate bundle. The image-sensor (active on creation) does this within ~15 min;
#    trigger it to skip the cron wait:
kubectl exec -n airflow deploy/airflow-scheduler -c scheduler -- \
  airflow dags trigger insurance_retention_image_sensor          # -> train reads bronze, registers a candidate

# 2. Gated-promote to @production via the promote DAG (atomic + emits the production Asset, which
#    AUTO-CASCADES the scoring DAG -> gold.selections now that bronze.score exists):
kubectl exec -n airflow deploy/airflow-scheduler -c scheduler -- \
  airflow dags trigger insurance_retention_promote              # gate nv_uplift_cv, set @production, fire the Asset
#    (ad-hoc alternative, metadata-only from any host with the tracking URI:)
#    MLFLOW_TRACKING_URI=https://mlflow.lab.batzbak.top python training/promote_bundle.py

# 3. Make serving load the freshly-promoted bundle (a promotion is an alias move, not a new image,
#    so ArgoCD does not roll the Deployment on its own):
kubectl rollout restart deploy/insurance-retention -n insurance-retention
```

Once `@production` exists (step 2) and serving has reloaded it (step 3), `/health` returns 200,
`gold.selections` holds the season's ranked top-200, and the batch selection equals the live
`/select`. Re-triggering any DAG registers new candidate versions / re-scores but never
auto-promotes; promotion stays the separate gated step, and is the only thing that moves
`@production`.

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
