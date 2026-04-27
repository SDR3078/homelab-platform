# ArgoCD Bootstrap

One-time manual install. After this runs, everything else in this repo is managed via ArgoCD.

## Pinned version

ArgoCD v3.3.8 (released April 2026, CNCF graduated).

## Disaster recovery

This file covers ArgoCD installation only. For a full from-scratch cluster
rebuild — which requires restoring the sealed-secrets master key BEFORE
applying the root Application — see [`docs/rebuild.md`](../docs/rebuild.md).

Skipping the master-key restore on rebuild leaves every sealed secret in
this repo permanently undecryptable.

## Step 1 — Install ArgoCD

```bash
# Create the namespace
kubectl create namespace argocd

# Install ArgoCD (non-HA, single-node appropriate)
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.3.8/manifests/install.yaml

# Wait for all ArgoCD Deployments to be ready (StatefulSet finishes shortly after)
kubectl wait --for=condition=available --timeout=300s \
  deployment --all -n argocd

# Verify all pods are Running
kubectl get pods -n argocd
```

Expected: 7 pods in the argocd namespace, all Running.

## Step 2 — Get the initial admin password

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d
```

Save this in a password manager. It's only needed for first login — after session 2
exposes ArgoCD via Ingress, we'll set a proper password and delete this secret.

## Step 3 — Port-forward to reach the UI (temporary)

In a separate terminal, leave this running:

```bash
kubectl port-forward -n argocd svc/argocd-server 8080:443
```

Then open https://localhost:8080 in a browser.

Log in:
- Username: `admin`
- Password: the value from Step 2

Accept the self-signed TLS warning. The UI will be empty (no Applications yet).

Once session 2 installs ingress-nginx + cert-manager, ArgoCD moves to
https://argocd.lab.<domain> and this port-forward is no longer needed.

## Step 4 — Bootstrap the GitOps loop

With ArgoCD running, apply the root Application:

```bash
kubectl apply -f bootstrap/root-app.yaml
```

Within 1-3 minutes, watch the ArgoCD UI:

1. `root` Application appears
2. `root` syncs and discovers `apps/test-app.yaml`
3. `whoami-test` Application appears
4. `whoami-test` syncs, creates the `whoami-test` namespace, deploys the whoami pod

Verify the whoami pod is running:

```bash
kubectl get pods -n whoami-test
```

Expected: one `whoami-xxxxx` pod, Running.

At this point the GitOps loop is live. Every future change flows through Git commits.

## Step 5 — Set a real admin password and delete the bootstrap secret

Once ArgoCD is reachable (initially via port-forward, later via Ingress
at https://argocd.lab.batzbak.top), swap the auto-generated initial
password for a real one and delete the bootstrap Secret.

```bash
# Log in with the initial password from Step 2:
argocd login localhost:8080 --username admin --insecure
# (later, behind Ingress: argocd login argocd.lab.batzbak.top)

# Set a new password (prompts twice):
argocd account update-password

# Verify the new password works:
argocd login localhost:8080 --username admin --insecure

# Delete the now-unused bootstrap Secret:
kubectl -n argocd delete secret argocd-initial-admin-secret
```

Save the new password to your password manager under
`homelab-platform / argocd-admin`. ArgoCD does NOT regenerate the
initial-admin-secret after deletion — recovery from a lost admin
password requires the in-band reset procedure documented at
https://argo-cd.readthedocs.io/en/stable/faq/#i-forgot-the-admin-password
(painful but possible).

## Why non-HA

The `install.yaml` (as opposed to `ha/install.yaml`) deploys a single-replica ArgoCD
suitable for single-node k3s. HA variant runs 3 replicas of each component, needs a
real Redis cluster, and is overkill for this setup.

## What gets installed

- argocd-server (API + UI)
- argocd-application-controller (reconciliation loop)
- argocd-repo-server (Git cloning + manifest rendering)
- argocd-redis (caching)
- argocd-dex-server (SSO, unused for now)
- argocd-notifications-controller
- argocd-applicationset-controller

## Next steps after this bootstrap

- Install sealed-secrets controller (separate bootstrap file, session 1 continuation)
- Session 2: expose ArgoCD via Ingress with TLS
- Session 2: replace placeholder whoami-test with real Applications (ingress-nginx, cert-manager)
