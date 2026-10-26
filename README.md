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
| `.github/workflows/deploy-grafana-monitoring.yaml` | Manually deploys Grafana Kubernetes Monitoring to GKE |

## Prerequisites

- A Google Cloud project with billing enabled and the GKE API enabled.
- An existing GKE Autopilot cluster.
- A GitHub account with Actions enabled for this repository.
- A Gemini API key and a random webhook token.
- Workload Identity Federation configured between GitHub Actions and Google Cloud.

> **Autopilot nodes:** Autopilot manages worker nodes; you do not create a node pool
> manually. An empty `kubectl get nodes` result by itself is not a reason to stop:
> this deployment workflow submits Argo CD workloads, which require Autopilot to
> provision capacity. The workflow waits for the Argo CD and agent Deployments to
> become Ready. If those rollouts time out, inspect Pending Pods and their events
> to identify the scheduling or provisioning issue.

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
| `GRAFANA_CLOUD_TOKEN` | Required for monitoring workflow | Grafana Cloud access-policy token for telemetry ingestion |

Never put credentials in manifests, source files, or committed workflow YAML.

For `GRAFANA_CLOUD_TOKEN`, use a Grafana Cloud access-policy token with the
`metrics:write` scope for the stack that owns the Prometheus remote-write
endpoint. This workflow writes directly to Prometheus; it does not use the
Grafana Fleet Management endpoint or its `set:alloy-data-write` scope. The
workflow passes the token to Helm at run time; Helm stores release data in a
Kubernetes Secret, so restrict access to secrets in the cluster.

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
4. The workflow authenticates to GCP, installs Argo CD, creates the
   `ai-agent-secrets` Secret, applies the Argo CD Application, and waits for the
   agent Deployment while Autopilot provisions capacity.

Check the result:

```bash
kubectl get pods -n argocd
kubectl get applications -n argocd
kubectl get pods,services -n ai-agent
kubectl rollout status deployment/ai-agent -n ai-agent
```

The Argo CD Application tracks the `main` branch. The build workflow pushes a new
agent image and updates its image tag in Git; Argo CD then syncs that manifest.

## Deploy Grafana Kubernetes Monitoring

1. In Grafana Cloud's Kubernetes Monitoring setup, select **GKE Autopilot** and
   install the recommended backend recording and alert rules.
2. Create an access-policy token for the same stack with the `metrics:write`
   scope. Add its value as the GitHub Actions secret `GRAFANA_CLOUD_TOKEN`; do
   not paste the token into the workflow or commit it.
3. Run **Actions → Deploy Grafana Kubernetes Monitoring** from `main`. Provide
   the full Prometheus remote-write URL and instance ID shown for your Grafana
   Cloud stack, plus a cluster label.
4. Check the deployment in Cloud Shell:

   ```bash
   helm list -n monitoring
   kubectl get pods -n monitoring
   kubectl get events -n monitoring --sort-by=.lastTimestamp
   ```

This workflow pins Grafana's `k8s-monitoring` chart to `4.5.5` and enables
Kubernetes cluster metrics only. It intentionally leaves host metrics, logs,
cost metrics, and other optional collectors disabled for a minimal Autopilot
setup. After metrics arrive, create or enable an alert rule in Grafana Cloud
Alerting and verify that it evaluates against this cluster's data.

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
