import base64, json, logging, os, re, urllib.request, urllib.error  

log = logging.getLogger("github_pr")  
API = "https://api.github.com"  
MAX_MEM_MI = 1024  # never raise memory above 1Gi  


def _gh(method, path, body=None):  
    req = urllib.request.Request(  
        API + path,  
        method=method,  
        data=json.dumps(body).encode() if body is not None else None,  
        headers={  
            "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],  
            "Accept": "application/vnd.github+json",  
            "Content-Type": "application/json",  
        },  
    )  
    with urllib.request.urlopen(req, timeout=20) as r:  
        txt = r.read().decode()  
        return json.loads(txt) if txt else {}  


def _mem_mi(v):  
    m = re.fullmatch(r"(\d+)(Mi|Gi)", str(v).strip().strip('"'))  
    if not m:  
        return None  
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)  


def _get_fix(diag):  
    patch = diag.get("patch") or diag.get("yaml_patch") or ""  
    image = memory = None  
    if isinstance(patch, dict):  
        image, memory = patch.get("image"), patch.get("memory") or patch.get("memory_limit")  
    else:  
        m = re.search(r'image:\s*"?([^\s"]+)', patch)  
        image = m.group(1) if m else None  
        m = re.search(r'memory:\s*"?(\d+(?:Mi|Gi))', patch)  
        memory = m.group(1) if m else None  
    return image, memory  


def _edit(text, dep, image, memory):  
    docs = text.split("\n---")  
    for i, d in enumerate(docs):  
        if re.search(r"kind:\s*Deployment", d) and re.search(r"name:\s*" + re.escape(dep) + r"\s*$", d, re.M):  
            new = d  
            if image:  
                new = re.sub(r"(image:\s*)\S+", lambda m: m.group(1) + image, new, count=1)  
            if memory:  
                new = re.sub(r"(limits:\s*\{[^}]*memory:\s*)[^,}\s]+", lambda m: m.group(1) + memory, new, count=1)  
                new = re.sub(r"(limits:\s*\n(?:[ \t]+\S.*\n)*?[ \t]+memory:\s*)\S+", lambda m: m.group(1) + memory, new, count=1)  
            if new != d:  
                docs[i] = new  
                return "\n---".join(docs)  
    return None  


def open_pr(*args, **kwargs):  
    ev = {}  
    for a in args:  
        if isinstance(a, dict):  
            ev.update(a)  
    ev.update(kwargs)  
    diag = ev.get("diagnosis") if isinstance(ev.get("diagnosis"), dict) else ev  
    repo = os.environ.get("GITHUB_REPO")  
    root = os.environ.get("GITOPS_ROOT", "apps/")  
    allowed = [n.strip() for n in os.environ.get("AUTO_PR_NAMESPACES", "default").split(",")]  
    ns, dep = ev.get("namespace"), ev.get("deployment")  

    if not repo or not os.environ.get("GITHUB_TOKEN"):  
        return log.info("PR skipped: GITHUB_REPO or GITHUB_TOKEN missing")  
    if ns not in allowed:  
        return log.info("PR skipped: namespace %s not allowed", ns)  
    if not dep:  
        return log.info("PR skipped: %s/%s is not part of a Deployment", ns, ev.get("pod"))  
    if str(diag.get("confidence", "")).lower() != "high":  
        return log.info("PR skipped: confidence is not high")  

    image, memory = _get_fix(diag)  
    if memory and (_mem_mi(memory) is None or _mem_mi(memory) > MAX_MEM_MI):  
        memory = None  
    if not image and not memory:  
        return log.info("PR skipped: no safe image/memory change for %s/%s", ns, dep)  

    try:  
        owner = repo.split("/")[0]  
        branch = f"ai-fix/{ns}-{dep}"  
        if _gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}"):  
            return log.info("PR skipped: a PR is already open for %s/%s", ns, dep)  

        base = _gh("GET", f"/repos/{repo}")["default_branch"]  
        base_sha = _gh("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]  
        tree = _gh("GET", f"/repos/{repo}/git/trees/{base_sha}?recursive=1")["tree"]  

        for f in tree:  
            p = f["path"]  
            if f["type"] != "blob" or not p.startswith(root) or not p.endswith((".yaml", ".yml")):  
                continue  
            c = _gh("GET", f"/repos/{repo}/contents/{p}?ref={base}")  
            text = base64.b64decode(c["content"]).decode()  
            new = _edit(text, dep, image, memory)  
            if not new:  
                continue  

            try:  
                _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})  
            except urllib.error.HTTPError as e:  
                if e.code != 422:  
                    raise  
                _gh("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")  
                _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})  

            change = ", ".join(x for x in [f"image={image}" if image else "", f"memory={memory}" if memory else ""] if x)  
            _gh("PUT", f"/repos/{repo}/contents/{p}", {  
                "message": f"AI fix for {ns}/{dep}: {change}",  
                "content": base64.b64encode(new.encode()).decode(),  
                "sha": c["sha"],  
                "branch": branch,  
            })  
            pr = _gh("POST", f"/repos/{repo}/pulls", {  
                "title": f"AI fix: {ev.get('issue', 'issue')} in {ns}/{dep}",  
                "head": branch,  
                "base": base,  
                "body": f"**Root cause:** {diag.get('root_cause')}\n\n**Fix:** {diag.get('fix')}\n\n**Change:** {change}\n\nOpened by ai-agent. Review before you merge.",  
            })  
            log.info("PR opened %s", pr.get("html_url"))  
            return pr.get("html_url")  

        log.info("PR skipped: no file under %s has Deployment %s", root, dep)  
    except urllib.error.HTTPError as e:  
        log.error("PR failed: HTTP %s %s", e.code, e.read().decode()[:300])  
    except Exception as e:  
        log.error("PR failed: %s", e)  
