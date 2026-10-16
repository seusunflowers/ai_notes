"""Probe opencode v2 server: banner, auth, routes, events, permission reply.

Throwaway script; kills its server with taskkill /T /F on exit.
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
APP_DATA = os.environ.get("APPDATA") or HOME
CLI = os.environ.get("TM_CLI") or os.path.join(APP_DATA, "npm", "opencode.cmd")
CWD = os.environ.get("TM_CWD") or os.path.join(os.environ.get("TEMP") or HOME, "opencode")
os.makedirs(CWD, exist_ok=True)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

env = os.environ.copy()
env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": {"*": "ask", "edit": "allow"}})

proc = subprocess.Popen(
    [CLI, "serve", "--hostname=127.0.0.1", "--port=0"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    cwd=CWD, env=env,
)
print("SERVER PID:", proc.pid, flush=True)

banner = []


def reader():
    try:
        for line in proc.stdout:
            banner.append(line.rstrip("\r\n"))
            print("BANNER|", re.sub(r"(server password )\S+", r"\1<redacted>", line.rstrip()), flush=True)
    except Exception:
        pass


threading.Thread(target=reader, daemon=True).start()

url = None
t0 = time.time()
while time.time() - t0 < 20:
    for b in banner:
        m = re.search(r"listening on\s+(https?://\S+)", b)
        if m:
            url = m.group(1).rstrip("/")
            break
    if url:
        break
    time.sleep(0.2)

if not url:
    print("NO URL FOUND", flush=True)
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(1)

print("URL:", url, flush=True)
time.sleep(1.0)
pw = None
for b in banner:
    m = re.search(r"password\s+(\S+)", b, re.I)
    if m:
        pw = m.group(1)
print("PASSWORD CAPTURED:", bool(pw), flush=True)

AUTH = {}
if pw:
    AUTH["Authorization"] = "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()
H = {"Accept": "application/json", **AUTH}
Q = "directory=" + urllib.parse.quote(CWD)


def http(method, path, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers if headers is not None else H)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            print(f"  {method} {path} -> {r.status} ct={r.headers.get('Content-Type')} {len(raw)}B", flush=True)
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        print(f"  {method} {path} -> HTTP {e.code} {raw[:300]!r}", flush=True)
        return e.code, raw
    except Exception as e:
        print(f"  {method} {path} -> ERR {e!r}", flush=True)
        return None, b""


def jhttp(method, path, body=None, **kw):
    st, raw = http(method, path, body, **kw)
    try:
        return st, json.loads(raw.decode("utf-8"))
    except Exception:
        return st, None


def unwrap(value):
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


print("--- auth mandatory check (no auth header) ---", flush=True)
st, raw = http("GET", "/api/info", headers={"Accept": "application/json"})
print("  no-auth status:", st, "body:", raw[:200], flush=True)

print("--- routes ---", flush=True)
st, info = jhttp("GET", f"/api/info?{Q}")
print("  info:", json.dumps(info)[:400], flush=True)

st, models = jhttp("GET", f"/api/model?{Q}")
entries = unwrap(models)
print("  model envelope keys:", list(models.keys()) if isinstance(models, dict) else type(models).__name__,
      "count:", len(entries) if isinstance(entries, list) else "?", flush=True)
if isinstance(entries, list) and entries:
    print("  first model keys:", list(entries[0].keys()), flush=True)
    print("  first model:", json.dumps(entries[0])[:300], flush=True)

st, prov = jhttp("GET", f"/api/provider?{Q}")
print("  provider:", json.dumps(prov)[:300], flush=True)

st, cred = jhttp("GET", f"/api/credential?{Q}")
cred_ids = []
if isinstance(cred, dict):
    data = cred.get("data")
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict):
                ident = c.get("id") or c.get("providerID") or c.get("name") or sorted(c.keys())[:4]
                cred_ids.append(ident)
    print("  credential envelope keys:", list(cred.keys()), flush=True)
else:
    print("  credential:", type(cred).__name__, flush=True)
print("  credentialed ids:", cred_ids, flush=True)

st, agents = jhttp("GET", f"/api/agent?{Q}")
print("  agents:", json.dumps(agents)[:300], flush=True)

st, spec_raw = http("GET", "/openapi.json", headers={"Accept": "application/json"})
spec = None
if st == 200:
    try:
        spec = json.loads(spec_raw.decode())
    except Exception:
        pass
if spec is None:
    st, spec_raw = http("GET", "/api/openapi.json", headers={"Accept": "application/json"})
    try:
        spec = json.loads(spec_raw.decode())
    except Exception:
        spec = None
if isinstance(spec, dict):
    paths = spec.get("paths", {})
    print("  OPENAPI paths:", len(paths), flush=True)
    perm_paths = [p for p in paths if "permission" in p]
    print("  permission paths:", perm_paths, flush=True)
    for p in perm_paths:
        if p.endswith("/reply"):
            node = paths[p]
            for meth, mnode in node.items():
                rb = mnode.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                print(f"  {meth.upper()} {p} body schema:", json.dumps(rb)[:600], flush=True)
    for p in ("/session/{sessionID}/prompt", "/session/{sessionID}/model", "/session/{sessionID}/agent", "/session/{sessionID}/revert", "/session/{sessionID}/interrupt"):
        if p in paths:
            for meth, mnode in paths[p].items():
                rb = mnode.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                print(f"  {meth.upper()} {p} body:", json.dumps(rb)[:500] if rb else "(none)", flush=True)
        else:
            print(f"  MISSING path {p}", flush=True)
else:
    print("  openapi not available as JSON", flush=True)

# --- pick a model ---
model_ref = None
if isinstance(entries, list):
    by_id = {}
    for m in entries:
        if not isinstance(m, dict) or not m.get("enabled", True):
            continue
        by_id[(m.get("providerID"), m.get("id"))] = m
    if ("deepseek", "deepseek-flash") in by_id:
        model_ref = by_id[("deepseek", "deepseek-flash")]
    else:
        for (prov_id, model_id), m in by_id.items():
            if prov_id in cred_ids:
                model_ref = m
                break
print("  SELECTED MODEL:", json.dumps(model_ref)[:200] if model_ref else None, flush=True)

# --- SSE listener ---
events = []
stop_sse = threading.Event()


def sse():
    req = urllib.request.Request(url + f"/api/event?{Q}",
                                 headers={"Accept": "text/event-stream", **AUTH}, method="GET")
    try:
        r = opener.open(req, timeout=120)
        buf = []
        while not stop_sse.is_set():
            line = r.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip("\r\n")
            if s == "":
                if buf:
                    raw = "\n".join(buf)
                    buf = []
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        ev = {"type": "PARSE?", "raw": raw[:200]}
                    events.append(ev)
                    d = ev.get("data")
                    print("EVENT|", ev.get("type"), "|",
                          json.dumps(d)[:250] if isinstance(d, (dict, list)) else str(d)[:250], flush=True)
                continue
            if s.startswith("data:"):
                buf.append(s[5:].lstrip())
    except Exception as e:
        print("SSE ended:", repr(e), flush=True)


threading.Thread(target=sse, daemon=True).start()
time.sleep(1.0)

print("--- session flow ---", flush=True)
st, created = jhttp("POST", f"/api/session?{Q}", {})
sid = None
sess = unwrap(created) if created is not None else None
if isinstance(sess, dict):
    sid = sess.get("id")
print("  session id:", sid, flush=True)

if sid:
    if model_ref:
        st, _ = jhttp("POST", f"/api/session/{sid}/model?{Q}",
                      {"model": {"id": model_ref.get("id"), "providerID": model_ref.get("providerID")}})
        print("  set model status:", st, flush=True)
    st, _ = jhttp("POST", f"/api/session/{sid}/agent?{Q}", {"agent": "build"})
    print("  set agent status:", st, flush=True)

    t1 = time.time()
    st, prompt_resp = jhttp("POST", f"/api/session/{sid}/prompt?{Q}",
                            {"text": "Use the shell tool to run exactly: echo TERMMATE_V2_OK . Then reply with the output and nothing else."},
                            timeout=300)
    print("  prompt status:", st, "elapsed:", round(time.time() - t1, 2), "s",
          "resp:", json.dumps(prompt_resp)[:200] if prompt_resp else None, flush=True)

    # wait for turn end; auto-reply to permission prompts
    replied = set()
    deadline = time.time() + 60
    while time.time() < deadline:
        for ev in list(events):
            if ev.get("type") == "permission.asked":
                d = ev.get("data") or {}
                pid = d.get("id")
                if pid and pid not in replied:
                    replied.add(pid)
                    print("  REPLYING to permission", pid, "action:", d.get("action"),
                          "resources:", json.dumps(d.get("resources"))[:150], flush=True)
                    st, raw = http("POST", f"/api/session/{sid}/permission/{urllib.parse.quote(str(pid), safe='')}/reply?{Q}",
                                   {"decision": "once"})
                    print("  permission reply status:", st, raw[:200], flush=True)
        if any(ev.get("type") in ("session.execution.succeeded", "session.execution.failed") for ev in events):
            break
        time.sleep(0.5)

    types = {}
    for ev in events:
        types[ev.get("type")] = types.get(ev.get("type"), 0) + 1
    print("  EVENT TYPE COUNTS:", json.dumps(types), flush=True)

    st, _ = jhttp("POST", f"/api/session/{sid}/interrupt?{Q}", {})
    print("  interrupt status:", st, flush=True)

    st, msgs = jhttp("GET", f"/api/session/{sid}/message?{Q}")
    data = unwrap(msgs)
    print("  message envelope keys:", list(msgs.keys()) if isinstance(msgs, dict) else None,
          "entries:", len(data) if isinstance(data, list) else "?", flush=True)
    if isinstance(data, list):
        for e in data[:4]:
            if isinstance(e, dict):
                print("   entry keys:", sorted(e.keys()), "type:", e.get("type"), flush=True)

    st, _ = jhttp("DELETE", f"/api/session/{sid}/revert?{Q}&messageID=msg_dummy", None)
    print("  revert DELETE status:", st, flush=True)
    st, _ = jhttp("GET", f"/api/session/{sid}?{Q}")
    print("  session get status:", st, flush=True)
else:
    print("  NO SESSION CREATED", flush=True)

stop_sse.set()
time.sleep(0.5)
subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
alive = subprocess.run(["tasklist", "/FI", f"PID eq {proc.pid}"],
                       capture_output=True, text=True).stdout
print("server alive after taskkill:", str(proc.pid) in alive, flush=True)
print("DONE", flush=True)
