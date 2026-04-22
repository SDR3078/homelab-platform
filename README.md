# Homelab Platform

GitOps-managed k3s cluster running data + ML platform services.

- `bootstrap/` — one-time manual ArgoCD install + root Application
- `apps/` — ArgoCD Application manifests, one per service
- `charts/` — Helm values per service
- `docs/` — architecture notes
