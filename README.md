# AI Kubernetes Troubleshooting Agent

An early-stage AI-assisted Kubernetes troubleshooting project. The agent receives
alerts, gathers evidence from affected Pods, asks Gemini for a diagnosis, displays
results in a web dashboard, and can optionally propose a GitOps change through a
GitHub pull request.

## How it works

1. Grafana polling or an authenticated alert webhook sends a firing alert to the agent.
2. The agent reads the Pod status, container details, recent events, and logs using
   its read-only Kubernetes service account.
3. The collected evidence is sent to Gemini for a diagnosis and suggested fix.
4. Results are available at `/ui` and `/diagnoses`.
5. For eligible high-confidence image or memory changes, the agent can open a GitHub
   pull request. Review and merge changes yourself; the agent does not directly
   modify running workloads.
6. Argo CD watches `apps/ai-agent` in this repository and syncs it to the cluster.

## Repository layout

| Path | Purpose |
| --- | --- |
| `agent/` | Python FastAPI agent, Gemini integration, dashboard, and optional PR logic |
| `apps/ai-agent/` | Kubernetes manifests for the agent |
| `argocd/ai-agent-app.yaml` | Argo CD Application pointing to `apps/ai-agent` |
| `.github/workflows/build-agent.yaml` | Builds and publishes the container when agent code changes |
| `.github/workflows/deploy-gke.yaml` | Manually installs Argo CD and deploys the agent to an existing GKE cluster |

## Prerequisites

- A Google Cloud project with billing enabled and the GKE API enabled.
- An existing GKE Autopilot cluster.
- A GitHub account with Actions enabled for this repository.
- A Gemini API key and a random webhook token.
- Workload Identity Federation configured between GitHub Actions and Google Cloud.

> **Cluster readiness:** Autopilot manages worker nodes, but it still needs to
> provision capacity for workloads. The deploy workflow checks for a Ready node
> and exits before installing anything if none is available. If `kubectl get nodes`
> returns no nodes and system Pods are Pending, resolve that GKE provisioning issue
> before deploying this application.

## Configure GitHub Actions authentication

The deploy workflow uses Workload Identity Federation (WIF), not a downloaded
service-account key. Run the following once in Cloud Shell, adjusting the project
or identifiers if needed:

```bash
PROJECT_ID=abdulproject-510307
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
POOL_ID=github-actions
PROVIDER_ID=ai-k8s-gitops
SA_NAME=gha-gke-deployer
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable \
  container.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="$PROJECT_ID"

gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT_ID"

# This role is needed to install Argo CD and its cluster-wide resources.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/container.admin"

gcloud iam workload-identity-pools create "$POOL_ID" \
  --project="$PROJECT_ID" \
  --location=global \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
  --project="$PROJECT_ID" \
  --location=global \
  --workload-identity-pool="$POOL_ID" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == 'techabdulrahman/ai-k8s-gitops' && assertion.ref == 'refs/heads/main'"

gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/techabdulrahman/ai-k8s-gitops"

echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
echo "$SA_EMAIL"
```

The deployer service account has broad Kubernetes administrator permissions because
the workflow installs Argo CD, which creates cluster-scoped resources. Use this
identity only for the protected `main` branch and review changes to the deployment
workflow carefully.

## Configure repository variables and secrets

In GitHub, open **Settings → Secrets and variables → Actions**.

Add these **repository variables**:

| Variable | Example |
| --- | --- |
| `GCP_PROJECT_ID` | `abdulproject-510307` |
| `GKE_CLUSTER` | `ai-k8s` |
| `GKE_LOCATION` | `asia-south1` |
| `GCP_SERVICE_ACCOUNT` | The service-account email printed above |

The deploy workflow pins the Workload Identity Provider resource name in
`.github/workflows/deploy-gke.yaml`; it is not a GitHub Actions variable.

Add these **repository secrets**:

| Secret | Required | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | Yes | Gemini API access |
| `WEBHOOK_TOKEN` | Yes | Authenticates alert webhook requests |
| `AI_AGENT_GITHUB_TOKEN` | No | Allows the agent to create GitHub pull requests |
| `GRAFANA_TOKEN` | No | Allows the agent to poll Grafana |

Never put credentials in manifests, source files, or committed workflow YAML.

## Deploy Argo CD and the agent

1. Confirm the cluster is healthy and has a Ready node:

   ```bash
   gcloud container clusters get-credentials ai-k8s \
     --region asia-south1 \
     --project abdulproject-510307
   kubectl get nodes
   kubectl get pods -A
   ```

2. Push the workflow and related changes to `main`.
3. In GitHub, open **Actions → Deploy Argo CD and AI Agent → Run workflow** and run
   it from `main`.
4. The workflow authenticates to GCP, checks cluster readiness, installs Argo CD,
   creates the `ai-agent-secrets` Secret, applies the Argo CD Application, and waits
   for the agent Deployment.

Check the result:

```bash
kubectl get pods -n argocd
kubectl get applications -n argocd
kubectl get pods,services -n ai-agent
kubectl rollout status deployment/ai-agent -n ai-agent
```

The Argo CD Application tracks the `main` branch. The build workflow pushes a new
agent image and updates its image tag in Git; Argo CD then syncs that manifest.

## Open the dashboard

Forward the agent Service to local port 8080:

```bash
kubectl port-forward -n ai-agent service/ai-agent 8080:80
```

Keep the command running and open <http://localhost:8080/ui>. Raw diagnosis data
is available at <http://localhost:8080/diagnoses>. If running `kubectl` in Cloud
Shell, use Cloud Shell's **Web Preview** for port 8080 instead of your local browser's
localhost.

## Costs and operational notes

- The GKE Free Tier credit applies to eligible cluster management fees; it does not
  necessarily cover Autopilot workload compute or other Google Cloud services.
- Autopilot charges for scheduled workloads according to their resource requests.
  Argo CD and the agent therefore may incur charges while running.
- The agent uses one replica with small resource requests. The Service is
  `ClusterIP`; use port forwarding rather than creating a public LoadBalancer.
- The agent keeps recent diagnoses in memory, so this history is lost when the Pod
  restarts.
- The dashboard shows the agent's application status, not a complete GKE or cluster
  health monitoring view.

## Current limitations

- The agent is a prototype. AI diagnoses and suggested changes require human review.
- The checked-in agent source currently contains literal `******` authorization
  placeholders in the webhook and Grafana request paths. Until those are replaced
  with the configured `WEBHOOK_TOKEN` and `GRAFANA_TOKEN`, those alert integrations
  will not authenticate as intended. Fix the source and build a new image before
  relying on alert intake or Grafana polling.
- The GitHub Container Registry image must be readable by the cluster. If the
  package is private, configure an image pull secret for GKE.
