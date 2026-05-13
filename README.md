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
- [ ] Lakekeeper + first pipeline (next)
- [ ] Full medallion + model registry
- [ ] Data quality + scheduling
- [ ] kube-prometheus-stack
- [ ] Pipeline & model observability
- [ ] LiteLLM + Langfuse + pgvector
- [ ] Portfolio chatbot as cluster app