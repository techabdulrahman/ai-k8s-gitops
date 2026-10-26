import os, json, logging, hashlib, threading, time, html, datetime, urllib.request, urllib.parse, urllib.error
from collections import deque  
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks  
from fastapi.responses import HTMLResponse, JSONResponse
from kubernetes import client, config  
from google import genai  
from github_pr import open_pr  

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")  
log = logging.getLogger("ai-agent")  

WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"].strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  
GRAFANA_URL = os.getenv("GRAFANA_URL", "").rstrip("/")  
GRAFANA_TOKEN = os.getenv("GRAFANA_TOKEN", "").strip()
GRAFANA_TOKEN_IS_CLOUD_POLICY = GRAFANA_TOKEN.startswith("glc_")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))  

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  
config.load_incluster_config()  
core = client.CoreV1Api()  

app = FastAPI()  
DIAGNOSES = deque(maxlen=50)  
SEEN = set()  
LOCK = threading.Lock()  
STARTED = time.time()  
LAST_POLL = {"time": None, "ok": None, "firing": 0}  


class _PrCapture(logging.Handler):  
    """Keeps the last 'PR ...' message from github_pr for the UI."""  
    def __init__(self):  
        super().__init__()  
        self.local = threading.local()  

    def emit(self, record):  
        msg = record.getMessage()  
        if msg.startswith("PR "):  
            self.local.msg = msg  


PR_CAPTURE = _PrCapture()  
logging.getLogger("github_pr").addHandler(PR_CAPTURE)  


def safe(fn, default="(not available)"):  
    try:  
        return fn()  
    except Exception as e:  
        return f"{default}: {e.__class__.__name__}"  


def collect(ns, pod, container):  
    p = safe(lambda: core.read_namespaced_pod(pod, ns), None)  
    ev = {"namespace": ns, "pod": pod, "container": container}  
    if p and not isinstance(p, str):  
        ev["phase"] = p.status.phase  
        ev["containers"] = [{"name": c.name, "image": c.image,  
                             "resources": c.resources.to_dict() if c.resources else None,  
                             "command": c.command, "args": c.args} for c in p.spec.containers]  
        ev["container_statuses"] = [{"name": s.name, "ready": s.ready, "restarts": s.restart_count,  
                                     "state": s.state.to_dict(), "last_state": s.last_state.to_dict()}  
                                    for s in (p.status.container_statuses or [])]  
        owner = (p.metadata.owner_references or [None])[0]  
        if owner and owner.kind == "ReplicaSet":  
            ev["owner"] = owner.name.rsplit("-", 1)[0]  
    events = safe(lambda: core.list_namespaced_event(ns, field_selector=f"involvedObject.name={pod}").items, [])  
    if isinstance(events, list):  
        ev["events"] = [f"{e.type} {e.reason} x{e.count}: {e.message}" for e in events[-15:]]  
    ev["logs_previous"] = safe(lambda: core.read_namespaced_pod_log(pod, ns, container=container or None, previous=True, tail_lines=40))  
    ev["logs_current"] = safe(lambda: core.read_namespaced_pod_log(pod, ns, container=container or None, tail_lines=20))  
    return ev  


PROMPT = """You are a Kubernetes SRE assistant. Diagnose ONE failing pod.  
Alert: {issue}  
Evidence (JSON):  
{evidence}  

Rules:  
- Use only the evidence. If it is not enough, say so.  
- Prefer pinned image tags (for example nginx:1.27), never "latest".  
- Answer in JSON with keys: root_cause, evidence (list of exact lines), fix,  
  yaml_patch (small YAML snippet or null), confidence (low|medium|high),  
  patch: an object with keys container, image, memory_limit, memory_request  
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
        resp = gemini.models.generate_content(  
            model=GEMINI_MODEL,  
            contents=PROMPT.format(issue=issue, evidence=json.dumps(evidence, default=str)[:12000]),  
            config={"response_mime_type": "application/json", "temperature": 0.1})  
        result = json.loads(resp.text)  
    except Exception as e:  
        log.exception("diagnosis failed")  
        result = {"error": str(e)[:300]}  
    record = {"source": source, "issue": issue, "namespace": ns, "pod": pod,  
              "deployment": evidence.get("owner"),  
              "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "diagnosis": result}  
    PR_CAPTURE.local.msg = None  
    try:  
        record["pr"] = open_pr(record, result.get("patch"))  
    except Exception as e:  
        log.exception("PR failed")  
        record["pr"] = f"error: {str(e)[:200]}"  
    record["pr_status"] = getattr(PR_CAPTURE.local, "msg", None)  
    DIAGNOSES.appendleft(record)  
    log.info("DIAGNOSIS %s", json.dumps(record))  


def fetch_firing_alerts():  
    """Grafana Alertmanager API: active, not silenced, ai_agent=true."""  
    q = urllib.parse.urlencode([("active", "true"), ("silenced", "false"),  
                                ("inhibited", "false"), ("filter", 'ai_agent="true"')])  
    req = urllib.request.Request(f"{GRAFANA_URL}/api/alertmanager/grafana/api/v2/alerts?{q}",  
                                 headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"})  
    with urllib.request.urlopen(req, timeout=15) as r:  
        return json.load(r)  


def poll_loop():  
    log.info("polling %s every %ss", GRAFANA_URL, POLL_SECONDS)  
    while True:  
        try:  
            alerts = [a for a in fetch_firing_alerts() if a.get("status", {}).get("state") == "active"]  
            LAST_POLL.update(time=time.time(), ok=True, firing=len(alerts))  
            current = {a["fingerprint"] for a in alerts}  
            with LOCK:  
                SEEN.intersection_update(current)  
                new = [a for a in alerts if a["fingerprint"] not in SEEN]  
                SEEN.update(a["fingerprint"] for a in new)  
            for a in new:  
                log.info("new alert %s %s/%s", a["labels"].get("alertname"),  
                         a["labels"].get("namespace"), a["labels"].get("pod"))  
                diagnose(a["labels"], "grafana-poll")  
        except urllib.error.HTTPError as e:
            LAST_POLL.update(time=time.time(), ok=False)
            endpoint_path = urllib.parse.urlparse(e.url).path
            if e.code == 401:
                hint = "invalid credentials or wrong token type"
            elif e.code == 403:
                hint = "service account role may be too low"
            else:
                hint = "request failed"
            log.warning("Grafana request %s returned HTTP %s: %s", endpoint_path, e.code, hint)
        except Exception as e:  
            LAST_POLL.update(time=time.time(), ok=False)  
            log.warning("poll failed: %s", e.__class__.__name__)
        time.sleep(POLL_SECONDS)  


@app.on_event("startup")  
def start_polling():  
    if GRAFANA_TOKEN_IS_CLOUD_POLICY:
        log.error(
            "GRAFANA_TOKEN is a Cloud access policy token; use a Grafana service account token (glsa_)"
        )
    elif GRAFANA_URL and GRAFANA_TOKEN:
        threading.Thread(target=poll_loop, daemon=True).start()  
    else:  
        log.warning("GRAFANA_URL/GRAFANA_TOKEN not set: polling disabled")  


@app.post("/webhook")  
async def webhook(req: Request, bg: BackgroundTasks, authorization: str = Header(default="")):  
    if authorization != f"Bearer {WEBHOOK_TOKEN}":  
        raise HTTPException(401)  
    body = await req.json()  
    for a in body.get("alerts", []):  
        if a.get("status") != "firing":  
            continue  
        key = a.get("fingerprint") or hashlib.sha1(json.dumps(a.get("labels"), sort_keys=True).encode()).hexdigest()  
        with LOCK:  
            if key in SEEN:  
                continue  
            SEEN.add(key)  
        bg.add_task(diagnose, a.get("labels", {}), "webhook")  
    return {"accepted": True}  


@app.get("/diagnoses")  
def diagnoses():  
    return list(DIAGNOSES)  


@app.get("/healthz")  
def healthz():  
    if GRAFANA_TOKEN_IS_CLOUD_POLICY:
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "error": "GRAFANA_TOKEN must be a Grafana service account token (glsa_), not a Cloud access policy token",
            },
        )
    return {"ok": True, "ready": True}


# ---------- Web UI ----------  

def _ist(ts):  
    if ts is None:  
        return "-"  
    if isinstance(ts, str):  
        try:  
            ts = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()  
        except Exception:  
            return ts  
    t = datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=5, minutes=30)))  
    return t.strftime("%d %b %H:%M:%S")  


def _pr_cell(r):
    pr, s = r.get("pr"), r.get("pr_status") or ""
    if isinstance(pr, str) and pr.startswith("https://"):
        return f'<a class="badge badge-green" href="{html.escape(pr, quote=True)}" target="_blank" rel="noopener noreferrer">View PR ↗</a>'
    if s.startswith("PR failed") or (isinstance(pr, str) and pr.startswith("error")):
        return f'<span class="badge badge-red">{html.escape(s or pr)}</span>'
    return f'<span class="badge badge-muted">{html.escape(s) or "No PR"}</span>'


@app.get("/ui", response_class=HTMLResponse)
def ui():
    rows = ""
    for r in DIAGNOSES:
        d = r.get("diagnosis") or {}
        conf = str(d.get("confidence", d.get("error", "")))
        conf_class = {"high": "badge-green", "medium": "badge-amber", "low": "badge-red"}.get(conf.lower(), "badge-muted")
        issue = html.escape(str(r.get("issue") or "Unknown issue"))
        pod = html.escape(f"{r.get('namespace') or '-'} / {r.get('pod') or '-'}")
        root_cause = html.escape(str(d.get("root_cause") or d.get("error") or "No diagnosis available"))
        fix = html.escape(str(d.get("fix") or "Review the evidence and logs."))
        rows += (
            f'<tr><td class="time-cell">{html.escape(_ist(r.get("time")))}</td>'
            f'<td><span class="issue-name">{issue}</span><span class="subtle">{html.escape(str(r.get("source") or "alert"))}</span></td>'
            f'<td class="pod-cell">{pod}</td>'
            f'<td class="detail-cell">{root_cause}</td><td class="detail-cell">{fix}</td>'
            f'<td><span class="badge {conf_class}">{html.escape(conf or "Unknown")}</span></td>'
            f'<td>{_pr_cell(r)}</td></tr>'
        )
    opened = sum(1 for r in DIAGNOSES if isinstance(r.get("pr"), str) and r["pr"].startswith("http"))
    up = str(datetime.timedelta(seconds=int(time.time() - STARTED)))
    poll_ok = LAST_POLL["ok"]
    poll, poll_class = ("Waiting", "badge-amber") if poll_ok is None else (("Connected", "badge-green") if poll_ok else ("Connection error", "badge-red"))
    last_poll = _ist(LAST_POLL["time"]) if LAST_POLL["time"] is not None else "Not yet polled"
    table_content = rows or '<tr><td colspan="7" class="empty-state"><span class="empty-icon">✦</span><strong>All quiet for now</strong><span>No diagnoses yet. When an alert arrives, the agent will investigate it here.</span></td></tr>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>AI K8s Agent Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg: #090e1a; --panel: #111a2b; --border: #202c42; --muted: #91a0b8; --text: #edf3ff; --cyan: #54d6e8; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: radial-gradient(ellipse at 15% -10%, #192e52 0, transparent 42%), var(--bg); color: var(--text); font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 34px 32px 48px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 30px; }}
    .brand {{ display: flex; align-items: center; gap: 14px; }}
    .logo {{ width: 48px; height: 48px; display: grid; place-items: center; border-radius: 15px; background: linear-gradient(135deg, #16c5d8, #6556e8); box-shadow: 0 8px 32px #10bcd533; font-size: 23px; }}
    h1 {{ margin: 0; font-size: 23px; letter-spacing: -.5px; }}
    .subtitle {{ display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .live {{ display: inline-flex; align-items: center; gap: 9px; border: 1px solid #245943; background: #102a27; color: #72e0ae; border-radius: 999px; padding: 8px 13px; font-size: 12px; font-weight: 700; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #48d597; box-shadow: 0 0 12px #48d597; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 24px; }}
    .card {{ position: relative; overflow: hidden; padding: 20px; border: 1px solid var(--border); border-radius: 16px; background: linear-gradient(145deg, #141f33, #101827); box-shadow: 0 10px 28px #0000001a; }}
    .card:after {{ content: ""; position: absolute; width: 90px; height: 90px; right: -36px; top: -44px; border-radius: 50%; background: var(--glow); filter: blur(24px); opacity: .42; }}
    .card-label {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
    .card-value {{ margin-top: 9px; font-size: 29px; line-height: 1.1; font-weight: 750; letter-spacing: -.7px; }}
    .card-foot {{ margin-top: 9px; color: #8190a8; font-size: 11px; }}
    .card.cyan {{ --glow: #23d3e8; }} .card.violet {{ --glow: #8c70ff; }} .card.orange {{ --glow: #ffad55; }} .card.green {{ --glow: #48d597; }}
    .card.cyan .card-value {{ color: #69deeb; }} .card.violet .card-value {{ color: #a99aff; }} .card.orange .card-value {{ color: #ffc078; }} .card.green .card-value {{ color: #72e0ae; }}
    .panel {{ overflow: hidden; border: 1px solid var(--border); border-radius: 16px; background: #101827; }}
    .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px 22px; border-bottom: 1px solid var(--border); }}
    h2 {{ margin: 0; font-size: 16px; }}
    .panel-meta {{ color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; text-align: left; }}
    th {{ padding: 12px 16px; color: #8e9cb2; background: #121d2e; font-size: 10px; letter-spacing: .09em; text-transform: uppercase; white-space: nowrap; }}
    td {{ padding: 15px 16px; border-top: 1px solid #1d293b; vertical-align: top; }}
    tbody tr:hover {{ background: #151f30; }}
    .time-cell, .pod-cell {{ color: #b3bfd2; white-space: nowrap; font-size: 12px; }}
    .issue-name {{ display: block; font-weight: 650; }} .detail-cell {{ min-width: 180px; max-width: 310px; color: #bdc8d9; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 11px; font-weight: 700; white-space: nowrap; }}
    .badge-green {{ color: #75e2b0; background: #15372f; border: 1px solid #245943; }}
    .badge-amber {{ color: #ffd184; background: #392c18; border: 1px solid #5e4724; }}
    .badge-red {{ color: #ff929b; background: #3c202a; border: 1px solid #61303e; }}
    .badge-muted {{ color: #a6b2c5; background: #202a3a; border: 1px solid #303d51; }}
    a {{ text-decoration: none; }} a:hover {{ filter: brightness(1.2); }}
    .empty-state {{ height: 205px; text-align: center; color: var(--muted); }}
    .empty-state strong, .empty-state span {{ display: block; }} .empty-state strong {{ color: var(--text); margin: 7px 0 3px; }} .empty-icon {{ color: var(--cyan); font-size: 27px; }}
    footer {{ display: flex; justify-content: space-between; gap: 12px; margin-top: 17px; color: #75839a; font-size: 11px; }}
    footer a {{ color: #72d9e7; }}
    @media (max-width: 850px) {{ .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .shell {{ padding: 24px 18px; }} }}
    @media (max-width: 520px) {{ header, .panel-head {{ align-items: flex-start; flex-direction: column; }} .cards {{ gap: 10px; }} .card {{ padding: 15px; }} .card-value {{ font-size: 24px; }} h1 {{ font-size: 20px; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div class="brand"><div class="logo">✦</div><div><h1>AI K8s Agent</h1><span class="subtitle">Kubernetes intelligence &amp; incident response</span></div></div>
      <div class="live"><span class="dot"></span> AGENT ONLINE</div>
    </header>
    <section class="cards" aria-label="Agent overview">
      <article class="card cyan"><div class="card-label">Firing alerts</div><div class="card-value">{LAST_POLL["firing"]}</div><div class="card-foot">Currently active in Grafana</div></article>
      <article class="card violet"><div class="card-label">Diagnoses</div><div class="card-value">{len(DIAGNOSES)}</div><div class="card-foot">Recent investigations</div></article>
      <article class="card orange"><div class="card-label">Pull requests</div><div class="card-value">{opened}</div><div class="card-foot">AI-proposed changes to review</div></article>
      <article class="card green"><div class="card-label">Agent uptime</div><div class="card-value">{html.escape(up)}</div><div class="card-foot">Model: {html.escape(GEMINI_MODEL)}</div></article>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Recent investigations</h2><div class="panel-meta">Grafana <span class="badge {poll_class}">{poll}</span> &nbsp; Last check: {html.escape(last_poll)} IST</div></div>
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Alert</th><th>Pod</th><th>Root cause</th><th>Recommended fix</th><th>Confidence</th><th>PR</th></tr></thead><tbody>{table_content}</tbody></table></div>
    </section>
    <footer><span>Refreshes automatically every 30 seconds</span><span><a href="/diagnoses">View raw diagnosis data →</a></span></footer>
  </main>
</body>
</html>"""
