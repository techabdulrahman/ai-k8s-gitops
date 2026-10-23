import os, json, logging, hashlib  
from collections import deque  
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks  
from kubernetes import client, config  
from google import genai  

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")  
log = logging.getLogger("ai-agent")  

WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]  
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")  
gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  

config.load_incluster_config()  
core = client.CoreV1Api()  
apps = client.AppsV1Api()  

app = FastAPI()  
DIAGNOSES = deque(maxlen=50)      # last 50 results, shown at /diagnoses  
SEEN = set()                      # avoid diagnosing the same alert twice  


def safe(fn, default="(not available)"):  
    try:  
        return fn()  
    except Exception as e:  
        return f"{default}: {e.__class__.__name__}"  


def collect(ns: str, pod: str, container: str) -> dict:  
    """Read-only evidence, the same steps a human would do."""  
    p = safe(lambda: core.read_namespaced_pod(pod, ns), None)  
    evidence = {"namespace": ns, "pod": pod, "container": container}  

    if p and not isinstance(p, str):  
        evidence["phase"] = p.status.phase  
        evidence["containers"] = [  
            {"name": c.name, "image": c.image,  
             "resources": c.resources.to_dict() if c.resources else None,  
             "command": c.command, "args": c.args}  
            for c in p.spec.containers  
        ]  
        evidence["container_statuses"] = [  
            {"name": s.name, "ready": s.ready, "restarts": s.restart_count,  
             "state": s.state.to_dict(), "last_state": s.last_state.to_dict()}  
            for s in (p.status.container_statuses or [])  
        ]  
        owner = (p.metadata.owner_references or [None])[0]  
        if owner and owner.kind == "ReplicaSet":  
            evidence["owner"] = owner.name.rsplit("-", 1)[0]  # Deployment name  

    events = safe(lambda: core.list_namespaced_event(  
        ns, field_selector=f"involvedObject.name={pod}").items, [])  
    if isinstance(events, list):  
        evidence["events"] = [  
            f"{e.type} {e.reason} x{e.count}: {e.message}" for e in events[-15:]  
        ]  

    evidence["logs_previous"] = safe(lambda: core.read_namespaced_pod_log(  
        pod, ns, container=container or None, previous=True, tail_lines=40))  
    evidence["logs_current"] = safe(lambda: core.read_namespaced_pod_log(  
        pod, ns, container=container or None, tail_lines=20))  
    return evidence  


PROMPT = """You are a Kubernetes SRE assistant. Diagnose ONE failing pod.  
Alert: {issue}  
Evidence (JSON):  
{evidence}  

Rules:  
- Use only the evidence. If it is not enough, say so.  
- Answer in JSON with keys:  
  root_cause (1-2 sentences),  
  evidence (list of the exact lines that prove it),  
  fix (what to change, in the Deployment YAML if possible),  
  yaml_patch (a small YAML snippet for the container, or null),  
  confidence (low|medium|high)  
"""  


def diagnose(alert: dict):  
    labels = alert.get("labels", {})  
    ns, pod = labels.get("namespace"), labels.get("pod")  
    if not ns or not pod:  
        log.warning("alert without namespace/pod: %s", labels)  
        return  
    issue = labels.get("issue") or labels.get("alertname", "unknown")  
    evidence = collect(ns, pod, labels.get("container", ""))  

    resp = gemini.models.generate_content(  
        model=GEMINI_MODEL,  
        contents=PROMPT.format(issue=issue, evidence=json.dumps(evidence, default=str)[:12000]),  
        config={"response_mime_type": "application/json", "temperature": 0.1},  
    )  
    try:  
        result = json.loads(resp.text)  
    except Exception:  
        result = {"raw": resp.text}  

    record = {"issue": issue, "namespace": ns, "pod": pod,  
              "deployment": evidence.get("owner"), "diagnosis": result}  
    DIAGNOSES.appendleft(record)  
    log.info("DIAGNOSIS %s", json.dumps(record))  


@app.post("/webhook")  
async def webhook(req: Request, bg: BackgroundTasks, authorization: str = Header(default="")):  
    if authorization != f"Bearer {WEBHOOK_TOKEN}":  
        raise HTTPException(401)  
    body = await req.json()  
    for a in body.get("alerts", []):  
        if a.get("status") != "firing":  
            continue  
        key = a.get("fingerprint") or hashlib.sha1(json.dumps(a.get("labels"), sort_keys=True).encode()).hexdigest()  
        if key in SEEN:  
            continue  
        SEEN.add(key)  
        bg.add_task(diagnose, a)          # answer Grafana fast, work in background  
    return {"accepted": True}  


@app.get("/diagnoses")  
def diagnoses():  
    return list(DIAGNOSES)  


@app.get("/healthz")  
def healthz():  
    return {"ok": True}  
