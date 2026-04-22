# ArgoCD Bootstrap

One-time manual install. After this runs, everything else in this repo is managed via ArgoCD.

## Pinned version

ArgoCD v3.3.8 (released 2026, CNCF graduated).

## Install commands

```bash
# Create the namespace
kubectl create namespace argocd

# Install ArgoCD (non-HA, single-node appropriate)
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.3.8/manifests/install.yaml

# Wait for all ArgoCD pods to be ready
kubectl wait --for=condition=available --timeout=300s \
  deployment --all -n argocd
```

## Get initial admin password

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d
```

Save this. Log in once to the UI (after ingress is set up in session 2), then this secret should be deleted.

## Why non-HA

The `install.yaml` (as opposed to `ha/install.yaml`) deploys a single-replica ArgoCD suitable for single-node k3s.
HA variant runs 3 replicas of each component, needs a real Redis cluster, and is overkill for this setup.

## What gets installed

- argocd-server (API + UI)
- argocd-application-controller (reconciliation loop)
- argocd-repo-server (Git cloning + manifest rendering)
- argocd-redis (caching)
- argocd-dex-server (SSO, unused for now)
- argocd-notifications-controller
- argocd-applicationset-controller

## Post-install

Next steps, handled in `bootstrap/`:
1. Install sealed-secrets controller
2. Expose ArgoCD via ingress (deferred to session 2)
3. Commit the root Application manifest
4. Apply the root Application to start the GitOps loop
