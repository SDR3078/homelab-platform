# Platform

GitOps-managed k3s cluster running data + ML platform services on batzbakserver.

## Structure

- `bootstrap/` — one-time manual ArgoCD install + root Application
- `apps/` — ArgoCD Application manifests, one per service
- `charts/` — Helm values per service
- `docs/` — architecture notes and session logs

## Cluster

Single-node k3s v1.34.6 running in a Proxmox VM on batzbakserver.

## Prerequisites for rebuild

To rebuild this cluster from scratch, you'll need:

- A Proxmox VE host (or any Linux host capable of running a VM)
- The `sealed-secrets-master-key-BACKUP.yaml` file — irreplaceable,
  stored OUTSIDE this repo. Without it, every committed sealed secret
  becomes permanently undecryptable
- A password manager with the entries listed in
  [`docs/rebuild.md`](docs/rebuild.md)
- DNS for `*.lab.batzbak.top` (or your equivalent) resolvable from the
  cluster, plus a Cloudflare API token for cert-manager's DNS-01 flow
- A devbox (any Linux workstation) with `kubectl`, `helm`, `argocd`,
  `kubeseal`, and `mc` CLIs installed

Full from-scratch rebuild runbook: [`docs/rebuild.md`](docs/rebuild.md).

## Services

To be populated as sessions progress:
- [x] k3s cluster bootstrapped
- [x] ArgoCD + sealed-secrets (session 1)
- [x] ingress-nginx + cert-manager (session 2)
- [x] CloudNativePG + MinIO (session 3)
- [x] Tenant data tier (postgres-tenants) + wedding-site migration (session 5)
- [x] Stakater Reloader for cross-repo Secret rollouts (session 6)
- [x] MLflow (session 7)
- [x] Airflow (session 8) — Dagster originally planned, switched after a
      cross-orchestrator comparison; see session-2026-05-13.md
- [x] Lakekeeper Iceberg REST Catalog (session 9) — API + read path
      validated; write path validated in the session-9 third checkpoint
      ("session 10") by fixing remote-signing at the source (warehouse
      `remote-signing-enabled: false`) + PyIceberg/FsspecFileIO. See
      session-2026-05-22.md.
- [x] insurance-retention MLOps showcase (2026-05-24/25): first real ML
      workload. A 3-model decision bundle trained by an Airflow DAG
      (KubernetesPodOperator) that a GHCR image-sensor auto-triggers on each new
      build via a data-aware Asset (code-driven CT, with image+commit lineage),
      versioned and gated-promoted in the MLflow
      registry, and served by an ArgoCD-managed FastAPI Deployment at
      https://insurance-retention.lab.batzbak.top. Chart in
      `charts/insurance-retention/`, DAG in `airflow/dags/`, recreation in
      `docs/rebuild.md` Step 12. Workload repo:
      https://github.com/SDR3078/insurance-retention
- [x] Iceberg data plane — medallion bronze.train / bronze.score →
      gold.selections via PyIceberg DAGs (platform-side notes sessions
      11–13, reconstructed 2026-06-08). Supersedes the planned "Trino +
      first Iceberg DAG": the write path was validated with PyIceberg, and
      Trino (1–2Gi always-on) was rejected for this single-node ML node —
      it returns only if an analytics-SQL consumer appears.
- [x] Model registry + gated promotion + in-cluster tuning lifecycle
      (MLflow aliases, NV non-regression gate, tune → promote-params → train)
- [ ] Data quality + scheduling (bronze DQ is minimal/structural today)
- [ ] kube-prometheus-stack (next — the platform now runs blind; overdue)
- [ ] Pipeline & model observability
- [ ] LiteLLM + Langfuse + pgvector
- [x] Portfolio chatbot as cluster app (2026-06) — Next.js web + FastAPI
      chat api, deployed on the wedding-site bootstrap+workload pattern from
      a private cross-repo chart. Hardened with the cluster's first
      least-privilege ArgoCD AppProject, a read-only GITHUB_TOKEN + Cloudflare
      Turnstile bot-check (SealedSecrets, resealed via `kubeseal --merge-into`),
      and a DeepInfra chat backend. Platform-side lessons in the course notes
      (session-2026-07-01); app-side in the portfolio-site course folder.