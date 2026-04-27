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
- [x] ArgoCD + sealed-secrets (session 1 completion)
- [x] ingress-nginx + cert-manager (session 2)
- [ ] CloudNativePG + MinIO (session 3)
- [ ] MLflow (session 4)
- [ ] Dagster (session 5)
- [ ] Lakekeeper + first pipeline (session 6)
- [ ] Full medallion + model registry (session 7)
- [ ] Data quality + scheduling (session 8)
- [ ] kube-prometheus-stack (session 9)
- [ ] Pipeline & model observability (session 10)
- [ ] LiteLLM + Langfuse + pgvector (session 11)
- [ ] Portfolio chatbot as cluster app (session 12)