import os, re, json, time, base64, logging, urllib.request, urllib.error  
import yaml  

log = logging.getLogger("ai-agent")  
REPO = os.getenv("GITHUB_REPO", "")  
BASE = os.getenv("GITHUB_BRANCH", "main")  
TOKEN = os.getenv("GITHUB_TOKEN", "")  
ROOT = os.getenv("GITOPS_ROOT", "apps/")  
ALLOWED_NS = {n.strip() for n in os.getenv("AUTO_PR_NAMESPACES", "default").split(",")}  
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$")   # must have a tag  
MEM_RE = re.compile(r"^(\d+)(Mi|Gi)$")  
MAX_MEM_MI = 1024  


def gh(method, path, body=None):  
    req = urllib.request.Request(  
        f"https://api.github.com/repos/{REPO}{path}", method=method,  
        data=json.dumps(body).encode() if body is not None else None,  
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",  
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})  
    with urllib.request.urlopen(req, timeout=20) as r:  
        data = r.read()  
        return json.loads(data) if data else {}  


def to_mi(v):  
    m = MEM_RE.match(str(v or ""))  
    if not m:  
        return None  
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)  


def find_manifest(deploy, ns):  
    tree = gh("GET", f"/git/trees/{BASE}?recursive=1")["tree"]  
    for item in tree:  
        p = item["path"]  
        if item["type"] != "blob" or not p.startswith(ROOT) or not p.endswith((".yaml", ".yml")):  
            continue  
        f = gh("GET", f"/contents/{p}?ref={BASE}")  
        docs = list(yaml.safe_load_all(base64.b64decode(f["content"]).decode()))  
        for d in docs:  
            md = (d or {}).get("metadata", {})  
            if d and d.get("kind") == "Deployment" and md.get("name") == deploy \  
                    and md.get("namespace", "default") == ns:  
                return p, f["sha"], docs  
    return None, None, None  


def apply_patch(docs, deploy, patch):  
    """Only image and memory changes are allowed."""  
    changes = []  
    for d in docs:  
        if not d or d.get("kind") != "Deployment" or d["metadata"]["name"] != deploy:  
            continue  
        containers = d["spec"]["template"]["spec"]["containers"]  
        c = next((c for c in containers if c["name"] == patch.get("container")), containers[0])  

        img = patch.get("image")  
        if img and img != c.get("image"):  
            if not IMAGE_RE.match(img) or img.endswith(":latest"):  
                log.warning("rejected image %s", img)  
            else:  
                changes.append(f"image: `{c.get('image')}` → `{img}`")  
                c["image"] = img  

        res = c.setdefault("resources", {})  
        for key, field in (("memory_limit", "limits"), ("memory_request", "requests")):  
            val = patch.get(key)  
            mi = to_mi(val)  
            if val and (mi is None or mi > MAX_MEM_MI):  
                log.warning("rejected %s=%s", key, val)  
                continue  
            if val and res.get(field, {}).get("memory") != val:  
                old = res.setdefault(field, {}).get("memory")  
                res[field]["memory"] = val  
                changes.append(f"{field}.memory: `{old}` → `{val}`")  
    return changes  


def open_pr(record, patch):  
    deploy, ns, issue = record.get("deployment"), record["namespace"], record["issue"]  
    if not (TOKEN and REPO and deploy and patch) or ns not in ALLOWED_NS:  
        return None  
    if record["diagnosis"].get("confidence") != "high":  
        log.info("PR skipped: confidence not high")  
        return None  

    open_prs = gh("GET", "/pulls?state=open&per_page=50")  
    if any(p["title"].startswith(f"fix({deploy})") for p in open_prs):  
        log.info("PR skipped: one is already open for %s", deploy)  
        return None  

    path, fsha, docs = find_manifest(deploy, ns)  
    if not path:  
        log.info("PR skipped: no manifest for %s/%s under %s", ns, deploy, ROOT)  
        return None  
    changes = apply_patch(docs, deploy, patch)  
    if not changes:  
        log.info("PR skipped: no safe change")  
        return None  

    branch = f"ai-fix/{deploy}-{issue.lower()}-{int(time.time())}"  
    base_sha = gh("GET", f"/git/ref/heads/{BASE}")["object"]["sha"]  
    gh("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})  
    content = yaml.safe_dump_all(docs, sort_keys=False)  
    gh("PUT", f"/contents/{path}", {"message": f"fix({deploy}): {issue}", "branch": branch,  
                                    "sha": fsha, "content": base64.b64encode(content.encode()).decode()})  

    dg = record["diagnosis"]  
    body = "\n".join([  
        f"## 🤖 AI fix for `{ns}/{deploy}` ({issue})",  
        f"**Root cause:** {dg.get('root_cause')}", "",  
        "**Evidence:**", *[f"- `{e}`" for e in dg.get("evidence", [])[:5]], "",  
        "**Changes:**", *[f"- {c}" for c in changes], "",  
        f"**Confidence:** {dg.get('confidence')}", "",  
        "_Generated by ai-agent. Review before merging. Argo CD deploys after merge._"])  
    pr = gh("POST", "/pulls", {"title": f"fix({deploy}): {issue} - {changes[0]}",  
                               "head": branch, "base": BASE, "body": body})  
    log.info("PR opened %s", pr["html_url"])  
    return pr["html_url"]  
