# Platform

GitOps-managed k3s cluster running data + ML platform services on batzbakserver.

## Structure

- `bootstrap/` — one-time manual ArgoCD install + root Application
- `apps/` — ArgoCD Application manifests, one per service
- `charts/` — Helm values per service
- `docs/` — architecture notes and session logs

## Cluster

Single-node k3s v1.34.6 running in a Proxmox VM on batzbakserver.

## Services

To be populated as sessions progress:
- [x] k3s cluster bootstrapped
- [ ] ArgoCD + sealed-secrets (session 1 completion)
- [ ] ingress-nginx + cert-manager (session 2)
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