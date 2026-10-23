import os, json, logging, hashlib, threading, time, html, datetime, urllib.request, urllib.parse  
from collections import deque  
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks  
from fastapi.responses import HTMLResponse  
from kubernetes import client, config  
from google import genai  
from `github_pr` import `open_pr`  

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")  
log = logging.getLogger("ai-agent")  

`WEBHOOK_TOKEN` = os.environ["`WEBHOOK_TOKEN`"]  
`GEMINI_MODEL` = os.getenv("`GEMINI_MODEL`", "gemini-3.5-flash-lite")  
`GRAFANA_URL` = os.getenv("`GRAFANA_URL`", "").rstrip("/")  
`GRAFANA_TOKEN` = os.getenv("`GRAFANA_TOKEN`", "")  
`POLL_SECONDS` = int(os.getenv("`POLL_SECONDS`", "60"))  

gemini = genai.Client(`api_key`=os.environ["`GEMINI_API_KEY`"])  
config.`load_incluster_config`()  
core = client.CoreV1Api()  

app = FastAPI()  
DIAGNOSES = deque(maxlen=50)  
SEEN = set()  
LOCK = threading.Lock()  
STARTED = time.time()  
`LAST_POLL` = {"time": None, "ok": None, "firing": 0}  


class _PrCapture(logging.Handler):  
    """Keeps the last 'PR ...' message from `github_pr` for the UI."""  
    def __init__(self):  
        super().__init__()  
        self.local = threading.local()  

    def emit(self, record):  
        msg = record.getMessage()  
        if msg.startswith("PR "):  
            self.local.msg = msg  


`PR_CAPTURE` = _PrCapture()  
logging.getLogger("`github_pr`").addHandler(`PR_CAPTURE`)  


def safe(fn, default="(not available)"):  
    try:  
        return fn()  
    except Exception as e:  
        return f"{default}: {e.__class__.__name__}"  


def collect(ns, pod, container):  
    p = safe(lambda: core.`read_namespaced_pod`(pod, ns), None)  
    ev = {"namespace": ns, "pod": pod, "container": container}  
    if p and not isinstance(p, str):  
        ev["phase"] = p.status.phase  
        ev["containers"] = [{"name": c.name, "image": c.image,  
                             "resources": c.resources.`to_dict`() if c.resources else None,  
                             "command": c.command, "args": c.args} for c in p.spec.containers]  
        ev["`container_statuses`"] = [{"name": s.name, "ready": s.ready, "restarts": s.`restart_count`,  
                                     "state": s.state.`to_dict`(), "`last_state`": s.`last_state`.`to_dict`()}  
                                    for s in (p.status.`container_statuses` or [])]  
        owner = (p.metadata.`owner_references` or [None])[0]  
        if owner and owner.kind == "ReplicaSet":  
            ev["owner"] = owner.name.rsplit("-", 1)[0]  
    events = safe(lambda: core.`list_namespaced_event`(ns, `field_selector`=f"involvedObject.name={pod}").items, [])  
    if isinstance(events, list):  
        ev["events"] = [f"{e.type} {e.reason} x{e.count}: {e.message}" for e in events[-15:]]  
    ev["`logs_previous`"] = safe(lambda: core.`read_namespaced_pod_log`(pod, ns, container=container or None, previous=True, `tail_lines`=40))  
    ev["`logs_current`"] = safe(lambda: core.`read_namespaced_pod_log`(pod, ns, container=container or None, `tail_lines`=20))  
    return ev  


PROMPT = """You are a Kubernetes SRE assistant. Diagnose ONE failing pod.  
Alert: {issue}  
Evidence (JSON):  
{evidence}  

Rules:  
- Use only the evidence. If it is not enough, say so.  
- Prefer pinned image tags (for example nginx:1.27), never "latest".  
- Answer in JSON with keys: `root_cause`, evidence (list of exact lines), fix,  
  `yaml_patch` (small YAML snippet or null), confidence (low|medium|high),  
  patch: an object with keys container, image, `memory_limit`, `memory_request`  
         (use null for values that should not change), or null if the fix is  
         not an image or memory change.  
- Memory values use Mi or Gi, for example "256Mi". Maximum 1Gi.  
"""  


def diagnose(labels, source):  
    ns, pod = labels.get("namespace"), labels.get("pod")  
    if not ns or not pod:  
        log.warning("alert without namespace/pod: %s", labels)  
        return  
    issue = labels.get("issue") or labels.get("alertname", "unknown")  
    evidence = collect(ns, pod, labels.get("container", ""))  
    try:  
        resp = gemini.models.`generate_content`(  
            model=`GEMINI_MODEL`,  
            contents=PROMPT.format(issue=issue, evidence=json.dumps(evidence, default=str)[:12000]),  
            config={"`response_mime_type`": "application/json", "temperature": 0.1})  
        result = json.loads(resp.text)  
    except Exception as e:  
        log.exception("diagnosis failed")  
        result = {"error": str(e)[:300]}  
    record = {"source": source, "issue": issue, "namespace": ns, "pod": pod,  
              "deployment": evidence.get("owner"),  
              "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "diagnosis": result}  
    `PR_CAPTURE`.local.msg = None  
    try:  
        record["pr"] = `open_pr`(record, result.get("patch"))  
    except Exception as e:  
        log.exception("PR failed")  
        record["pr"] = f"error: {str(e)[:200]}"  
    record["`pr_status`"] = getattr(`PR_CAPTURE`.local, "msg", None)  
    DIAGNOSES.appendleft(record)  
    log.info("DIAGNOSIS %s", json.dumps(record))  


def `fetch_firing_alerts`():  
    """Grafana Alertmanager API: active, not silenced, `ai_agent`=true."""  
    q = urllib.parse.urlencode([("active", "true"), ("silenced", "false"),  
                                ("inhibited", "false"), ("filter", '`ai_agent`="true"')])  
    req = urllib.request.Request(f"{`GRAFANA_URL`}/api/alertmanager/grafana/api/v2/alerts?{q}",  
                                 headers={"Authorization": f"Bearer {`GRAFANA_TOKEN`}"})  
    with urllib.request.urlopen(req, timeout=15) as r:  
        return json.load(r)  


def `poll_loop`():  
    log.info("polling %s every %ss", `GRAFANA_URL`, `POLL_SECONDS`)  
    while True:  
        try:  
            alerts = [a for a in `fetch_firing_alerts`() if a.get("status", {}).get("state") == "active"]  
            `LAST_POLL`.update(time=time.time(), ok=True, firing=len(alerts))  
            current = {a["fingerprint"] for a in alerts}  
            with LOCK:  
                SEEN.`intersection_update`(current)  
                new = [a for a in alerts if a["fingerprint"] not in SEEN]  
                SEEN.update(a["fingerprint"] for a in new)  
            for a in new:  
                log.info("new 
