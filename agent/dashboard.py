import collections, datetime, html, json, logging  
from fastapi.responses import HTMLResponse  

EVENTS = collections.deque(maxlen=100)  
STARTED = datetime.datetime.utcnow()  
_last_pr = {"msg": None}  


class _Capture(logging.Handler):  
    def emit(self, record):  
        msg = record.getMessage()  
        if msg.startswith("PR "):  
            _last_pr["msg"] = msg  
        elif msg.startswith("DIAGNOSIS "):  
            try:  
                ev = json.loads(msg[len("DIAGNOSIS "):])  
            except Exception:  
                return  
            ev["pr_status"] = _last_pr["msg"]  
            _last_pr["msg"] = None  
            EVENTS.appendleft(ev)  


def _ist(ts):  
    try:  
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")  
        return (t + datetime.timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M:%S")  
    except Exception:  
        return ts or ""  


def _pr_cell(s):  
    if not s:  
        return "-"  
    if s.startswith("PR opened "):  
        url = html.escape(s.split()[-1])  
        return f'PR opened'  
    cls = "bad" if s.startswith("PR failed") else "skip"  
    return f'{html.escape(s)}'  


def attach(app):  
    root = logging.getLogger()  
    if not root.handlers:  
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")  
    root.addHandler(_Capture())  

    @app.get("/ui", response_class=HTMLResponse)  
    def ui():  
        rows = ""  
        for e in EVENTS:  
            d = e.get("diagnosis") or {}  
            rows += (  
                ""  
                f"{_ist(e.get('time'))}"  
                f"{html.escape(str(e.get('issue')))}"  
                f"{html.escape(str(e.get('namespace')))}/{html.escape(str(e.get('pod')))}"  
                f"{html.escape(str(d.get('root_cause', '')))}"  
                f"{html.escape(str(d.get('fix', '')))}"  
                f"{html.escape(str(d.get('confidence', '')))}"  
                f"{_pr_cell(e.get('pr_status'))}"  
                ""  
            )  
        opened = sum(1 for e in EVENTS if (e.get("pr_status") or "").startswith("PR opened"))  
        up = str(datetime.datetime.utcnow() - STARTED).split(".")[0]  
        return f"""  
  
  
AI K8s Agent ● Running  
  
Uptime{up}  
Diagnoses{len(EVENTS)}  
PRs opened{opened}  
  
  
{rows or ''}  
Time (IST)IssuePodRoot causeFixConfidencePRNo events yet. The cluster is healthy.Auto-refresh every 30s."""  
