# Patching TermMate (Sublime Text 4) for OpenCode v2

A reproducible guide for AI agents and humans who need to make the
[TermMate](https://termmate.app) Sublime Text 4 package work with the OpenCode
v2 CLI. Written from a real Windows repair; the analysis and the patch are
platform-agnostic, and platform-specific differences are called out inline.

**Status:** verified working end-to-end against `opencode v2.0.27`.
**Scope:** `genfoundry/opencode_agent.py` only. No other TermMate file needed edits.

---

## 0. TL;DR

OpenCode v2 is a ground-up rewrite (new Go binary, new package name, new HTTP
API). TermMate's adapter was written against v1's Python-era API and fails in
**four independent ways** at once. One of them — the startup banner — fails
*before* the model fetch and the event stream are even started, which is why a
single symptom ("it connects but shows nothing") actually has four root causes.

The fix is additive: detect the API version once per connection, then branch.

---

## 1. Symptoms and the mental model

What the user sees:

```
- The agent label renders correctly ("opencode: (OpenCode by Anomaly)")
- The model picker is empty
- No content is ever produced
```

Why the label still appears: the label comes from a **separate `--version`
probe** that only checks that the CLI binary is on disk. It never touches the
server. So "the agent is detected" tells you nothing about whether the
integration works.

**The single most important structural fact** — in `OpenCodeAgent.connect()`:

```python
async def connect(self):
    self._loop = asyncio.get_running_loop()
    env = self._runtime_env()
    try:
        self.server_url = await self._start_server(env)   # <-- FAILS HERE
        await self._finish_connect(env, prompt)
    except Exception:
        await self.disconnect()
        raise
```

and `_finish_connect()` is where the SSE stream and the model fetch are launched:

```python
async def _finish_connect(self, env, prompt):
    await self._http("GET", "/global/health", include_directory=False)  # v1-only route
    self._is_connected = True
    self._event_task = asyncio.create_task(self._process_events())
    self._start_sse_thread(env)
    ready = await self._loop.run_in_executor(None, lambda: self._sse_ready.wait(timeout=5.0))
    if not ready:
        raise RuntimeError("Timed out connecting to the OpenCode event stream")
    asyncio.create_task(self._fetch_models())     # <-- models
    if prompt:
        await self.send_message(prompt)
```

Because the banner parse happens *upstream* of both, one regex failure kills
the model list **and** all streaming together. Fixing only the banner surfaces
the next three bugs in sequence. Expect a cascade, not a single defect.

---

## 2. The four breakages

| # | Area | v1 (what TermMate expects) | v2 (what it gets) |
|---|------|----------------------------|-------------------|
| 1 | Startup banner | `opencode server listening on <url>` | `server listening on <url>` — **`opencode ` prefix gone** |
| 2 | API root | `/global/health`, `/provider`, `/session` | everything under **`/api`** |
| 3 | Auth | optional `OPENCODE_SERVER_PASSWORD` env var | **mandatory** Basic auth; password printed only on the banner |
| 4 | Event vocabulary | `message.part.delta`, `message.part.updated`, `session.idle` | `session.text.delta`, split `session.tool.*`, `session.execution.succeeded` |

Plus route renames and envelope changes in §5.

The nastiest trap in the whole exercise:

> **Unprefixed paths on v2 return `200 OK` with the SPA HTML, not a 404.**

An unknown route silently returns the web UI's `index.html` (7164 bytes,
`text/html`). A naive client sees "success" and parses garbage. When probing,
always assert on **content type and shape**, never on status code alone. This is
why `/api/provider` "worked" in early manual probing while returning the
homepage.

---

## 3. Research methodology (do this before writing code)

Do not guess the v2 API from memory. In order of value:

1. **Read the adapter you are patching.** Fetch the upstream file listing, then
   read `opencode_agent.py` end to end. Its exact line numbers and control flow
   determine where the version branch belongs.
   - GitHub API tree: `https://api.github.com/repos/<owner>/<repo>/git/trees/main?recursive=1`
   - Raw file: `https://raw.githubusercontent.com/<owner>/<repo>/main/<path>`
2. **Run the v2 server by hand and read `/openapi.json`.** This is the
   authoritative spec and it is served by the binary itself. It answers every
   question about routes, request bodies, and enums in one shot.
3. **Subscribe to the SSE stream and dump real events.** The OpenAPI spec
   describes schemas; it will not tell you the wire format of the event
   envelope or which events actually fire during a real turn. Record a full
   turn including a shell tool call and a permission prompt.
4. **Probe cold and warm.** v2 resolves provider credentials asynchronously;
   `/api/model` legitimately returns `data: []` for the first few seconds. Any
   model-fetch patch needs a retry, or the picker will be intermittently empty.

---

## 4. Environment constraints you will hit

These cost real time during the original repair; record them so the next agent
does not rediscover them.

| Constraint | Detail |
|---|---|
| **Long-lived servers kill the agent shell** | `opencode serve` never exits. `Start-Process -NoNewWindow`, `Start-Job`, and foreground invocation all abort the tool call, because the harness waits on descendant processes and inherited handles. |
| **Solution** | Launch with the shell tool's `background=true` and redirect output to a file (`1> file 2>&1`), then read the file in a *separate* foreground command. For anything more complex, write a Python script that spawns, probes, and kills the server inside one foreground invocation. |
| **Never `Stop-Process -Name opencode`** | If the agent harness itself runs on OpenCode, this kills your own session. Target a specific PID captured at spawn time, or never kill by name. |
| **PowerShell execution policy** | `npm.ps1` / `opencode.ps1` may be blocked (`PSSecurityException`). Use the `.cmd` shim: `npm.cmd`, `opencode.cmd`. |
| **git egress may be proxy-blocked** | `git clone https://github.com/...` can fail with a proxy connect error while `webfetch`/HTTP works. Use the GitHub API + `raw.githubusercontent.com` instead. |
| **Paths with spaces** | Quote every path in PowerShell: `& "C:\Program Files\Python314\python.exe"`. |

---

## 5. Complete v1 → v2 route and event mapping

Derived from `/openapi.json` and live SSE capture.

### Routes

| Concern | v1 | v2 |
|---|---|---|
| Health / version | `GET /global/health` | `GET /api/info` → `{version, pid, urls, paths, capabilities}` |
| Models | `GET /provider` → `{connected, all:[{id,name,models}]}` | `GET /api/model` → `{location, data:[Model.Info]}` |
| Sessions | `GET /session` | `GET /api/session` |
| Create session | `POST /session` | `POST /api/session` |
| Send turn | `POST /session/{id}/prompt_async` body `{parts, agent, model, system, tools}` | `POST /api/session/{id}/prompt` body **`{text}`** — blocks for the whole turn; **no `system` field**; `prompt_async` is 404 |
| Select model | part of the prompt body | `POST /api/session/{id}/model` body `{"model": {id, providerID, variant?}}` → 204. Note `Model.Ref` uses **`id`**, not `modelID` |
| Select agent | `agent` field in prompt body | `POST /api/session/{id}/agent` body `{"agent": "<name>"}` |
| Interrupt | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |
| Revert | `POST /session/{id}/revert` body `{messageID}` | `DELETE /api/session/{id}/revert` — **no message selector**, truncates to session start |
| Permissions | `POST /session/{id}/permissions/{pid}` | `POST /api/session/{id}/permission/{requestID}/reply` body `{"decision":"once"\|"always"\|"reject"}` → 204 |
| Events | `GET /event` | `GET /api/event` |

Enums: `Permission.Reply = ["once","always","reject"]`,
`Permission.Effect = ["allow","deny","ask"]`.
`Model.Info` fields: `id, modelID, providerID, name, capabilities, variants, time, cost, status, enabled, limit`.

### Response envelopes

```
v1: { ... }                                  bare
v2: {location: {directory}, data: [...]}      lists  (/api/provider, /api/model, /api/agent, /api/session)
v2: {data}                                    single object, POST replies
v2: {data, cursor: {previous, next}}          paged  (/api/session/{id}/message)
```

### SSE events

v2 envelope — note the payload key is **`data`**, not `properties`, and the
session id is `data.sessionID`:

```json
{"id":"evt_…","created":<ms>,"type":"…","location":{"directory":"…"},
 "data":{…},"durable":{"aggregateID","seq","version"}}
```

Plus bare `: heartbeat` comment lines, which a naive line-reader must skip.

| v2 event | `data` | Maps to |
|---|---|---|
| `server.connected` | `{}` | connection ready |
| `session.renamed` | `{sessionID, title}` | session title |
| `session.usage.updated` | `{sessionID, cost, tokens:{input,output,reasoning,cache:{read,write}}}` | usage display |
| `session.step.started` | `{sessionID, agent, model:{id,providerID,variant}, assistantMessageID, started}` | turn start |
| `session.text.started` / `.delta` / `.ended` | `{sessionID, assistantMessageID, ordinal, delta}` | **text streaming** (`.delta` is the hot path) |
| `session.tool.input.started` | `{sessionID, assistantMessageID, id:"call_…", name}` | tool start; terminal tool is named **`shell`** |
| `session.tool.input.ended` | `{…, id, text}` | raw input JSON |
| `session.tool.called` | `{…, id, input:{command:…}, executed:false}` | **input available here** |
| `session.tool.progress` | `{…, id, metadata:{shellID}}` | running |
| `session.tool.success` | `{…, id, content:[{type:"text",text}], metadata:{status,truncated}}` | tool complete |
| `session.step.streamed` / `.ended` | `{finish:"stop"\|"tool-calls", rawFinish, cost, tokens}` | step boundary |
| `session.execution.succeeded` | `{sessionID}` | **turn end — emit STOP** |
| `session.execution.failed` | `{sessionID, error:{type, message, status, response:{body}}}` | **turn end — emit ERROR** |
| `permission.asked` | `{id:"per_…", sessionID, action:"shell", resources:[…], save:[…], source:{type,messageID,id}}` | approval prompt |
| `permission.replied` | `{sessionID, requestID, reply}` | approval echo |
| `shell.created` / `shell.exited` | `{info:{id,status,command,cwd,…}}` / `{id, exit, status}` | PTY lifecycle |

**Key structural difference:** v1 reported one tool call as one event with a
complete input and output. v2 splits it across **three to five** events
(`input.started` → `called` → `progress` → `success`/`error`), and the input is
only complete at `.called`. A correct v2 handler must **accumulate state keyed
by call id** until the terminal event arrives, then emit one completed tool
message. Emitting on `input.started` yields tools with empty output.

**Permission config is unchanged.** `OPENCODE_CONFIG_CONTENT` with
`{"permission":{"*":"ask","edit":"allow"}}` behaves identically on v1 and v2,
and merges as an overlay rather than replacing the user's `opencode.json`
(verify with `opencode debug config`).

---

## 6. The patch

Additive throughout: every v1 path is preserved and still takes priority when
running against an old server. One flag, `_api_v2`, detected once per
connection, drives everything.

### 6.1 Banner + password capture

```python
# v1 announces itself with an "opencode" prefix; v2 omits it.
_SERVER_URL_RE = re.compile(
    r"(?:opencode\s+)?server listening on\s+(https?://[^\s]+)", re.IGNORECASE
)
_SERVER_PASSWORD_RE = re.compile(r"server password\s+(\S+)", re.IGNORECASE)
_V1_BANNER_RE = re.compile(r"\s*opencode\s+server listening on\b", re.IGNORECASE)

def _is_v2_banner(line: str) -> bool:
    return not _V1_BANNER_RE.match(line)
```

In `_read_server_url()`, on a URL match, if the line looks like a v2 banner,
read the *next* line for the password before returning:

```python
match = _SERVER_URL_RE.search(line)
if match:
    if _is_v2_banner(line):
        self._server_password = await self._read_server_password()
    return match.group(1)
```

`_read_server_password(timeout=3.0)` loops `readline()` under
`asyncio.wait_for` against a `loop.time() + timeout` deadline, returning `None`
on EOF or timeout. **It must not raise** — some deployments print no password.

### 6.2 Version detection

Replace the v1 health probe with a single unprefixed probe. `/api/info` exists
only on v2, so it is a reliable discriminator, and it needs no auth guess:

```python
async def _detect_api_version(self):
    self._api_v2 = False
    try:
        info = await self._http("GET", "/api/info", include_directory=False)
    except Exception as error:
        LOG.debug("OpenCode /api/info probe failed: %s", error)
        info = None
    if isinstance(info, dict) and info.get("version"):
        self._api_v2 = True
        LOG.info("Detected OpenCode v2 server (version %s)", info["version"])
        return
    await self._http("GET", "/global/health", include_directory=False)
    LOG.info("Detected OpenCode v1 server")
```

### 6.3 Auth

```python
password = self._server_password or source.get("OPENCODE_SERVER_PASSWORD")
```

Banner password takes priority; the env var remains the override for v1 and for
pre-started servers. Everything after this is unchanged (Basic
`username:password`, default username `opencode`).

### 6.4 Routing and envelope

```python
# _build_url
if self._api_v2 and not path.startswith("/api"):
    path = "/api" + path
```

```python
# _unwrap — v2 adds "location" (project context) and "cursor" (paging) to the envelope
@staticmethod
def _unwrap(value):
    if isinstance(value, dict) and "data" in value and set(value).issubset({"data", "error", "location", "cursor"}):
        if value.get("error"):
            raise RuntimeError(OpenCodeAgent._error_text(value["error"]))
        return value.get("data")
    return value
```

### 6.5 Model discovery with warm-up retry

v2 resolves provider credentials asynchronously; `/api/model` returns
`data: []` for the first few seconds. Retry, or the picker is intermittently
empty. **This matters more than it looks** — without it the patch looks broken
on first run and fine on the second, which is a miserable thing to debug.

```python
_V2_MODEL_ATTEMPTS = 8
_V2_MODEL_RETRY_DELAY = 1.0

async def _fetch_models_v2(self):
    for attempt in range(_V2_MODEL_ATTEMPTS):
        result = await self._http("GET", "/model")
        entries = result.get("data") if isinstance(result, dict) else result
        models = []
        for model in entries or []:
            if not model.get("enabled", True):
                continue
            provider_id = model.get("providerID")
            model_id = model.get("modelID") or model.get("id")
            if not provider_id or not model_id:
                continue
            models.append({
                "displayName": model.get("name") or model_id,
                "description": provider_id,
                "value": f"{provider_id}/{model_id}",
            })
        if models:
            return models
        await asyncio.sleep(_V2_MODEL_RETRY_DELAY)
    return []
```

`value` stays `providerID/modelID` — TermMate's own settings and status bar
already use that format, so keep it for backward compatibility with any model
the user has already saved.

### 6.6 Event dispatch

Branch at the top of `_dispatch_event`:

```python
if self._api_v2:
    await self._dispatch_event_v2(event)
    return
```

`_dispatch_event_v2` reads `type` and `data` (non-dict → `{}`), filters on
`data.sessionID` against the active session, then handles the events in §5.
Store per-tool state on `self._pending_tools[call_id]` as events arrive:

```python
session.text.delta        -> await self._emit_text_delta(data.get("delta"), data.get("assistantMessageID"))
session.tool.input.started-> self._pending_tools[call_id] = {"name": data.get("name") or "tool"}
session.tool.called       -> self._pending_tools.setdefault(call_id, {})["input"] = data.get("input")
session.tool.success/error-> await self._finish_v2_tool(event_type, data)
permission.asked          -> await self._handle_v2_permission(data)
session.execution.succeeded -> await self._finish_turn()
session.execution.failed  -> enqueue Message(MessageType.ERROR, ...); reset turn state; self._turn_done.set()
```

`_finish_v2_tool` deduplicates against `self._terminal_tools` (events can
replay), pops the pending entry, joins the `content` text chunks, and calls
the existing `_emit_completed_tool` with a v1-shaped state dict:

```python
state = {
    "status": "error" if failed else "completed",
    "input": pending.get("input") or {},
    "output": output,
    "error": data.get("error") if failed else None,
    "metadata": data.get("metadata") or {},
}
await self._emit_completed_tool(
    {"id": call_id, "tool": pending.get("name") or "tool"}, state
)
```

Reusing v1's `_emit_completed_tool` means the whole downstream rendering,
approval, and artifact pipeline keeps working untouched. **Do not rewrite the
renderer** — that is where scope explosion lives.

Add `"shell": "Bash"` to `_TOOL_NAMES`: v2 renamed the terminal tool.

### 6.7 Turn submission

`POST /api/session/{id}/prompt` **blocks for the entire turn** while SSE
carries the output. Awaiting it inline deadlocks against the event stream, so
fire it as a background task:

```python
if self._api_v2:
    await self._apply_v2_session_settings(session_id, agent_name)
    asyncio.create_task(self._prompt_v2(session_id, content))
else:
    await self._http("POST", f"/session/{sid}/prompt_async", body=body)
```

```python
async def _apply_v2_session_settings(self, session_id, agent_name):
    await self._http("POST", f"/session/{session_id}/agent", body={"agent": agent_name})
    model = self._model_payload()
    if model:
        # v2 Model.Ref uses "id", not "modelID"
        await self._http("POST", f"/session/{session_id}/model",
                         body={"model": {"id": model["modelID"], "providerID": model["providerID"]}})
```

Both failures here are non-fatal — log at debug and continue.

`_prompt_v2` must close the turn on transport failure, or `send_message()`
deadlocks forever waiting on `_turn_done`:

```python
try:
    await self._http("POST", f"/session/{sid}/prompt", body={"text": content})
except Exception as error:
    if self._turn_active:
        self._turn_active = False
        self._turn_done.set()
        enqueue(Message(MessageType.ERROR, content=str(error)))
```

### 6.8 Permission replies

```python
if self._api_v2:
    body = {"decision": response}          # "once" | "reject"
    if response == "reject" and response_data.get("message"):
        body["message"] = response_data["message"]
    await self._http("POST", f"/session/{sid}/permission/{permission_id}/reply", body=body)
    return
```

### 6.9 Renamed verbs

```python
verb = "interrupt" if self._api_v2 else "abort"          # interrupt()
await self._http("POST", f"/session/{sid}/{verb}")
```

Rewind: v2 uses `DELETE /api/session/{id}/revert` and exposes **no message
selector**, so it truncates to session start. Use
`query={"messageID": message_id}` and accept the degraded behaviour.

### 6.10 The synchronous session picker

`list_opencode_sessions()` and `get_opencode_session_info()` spin up their own
`_SyncOpenCodeServer` instance and must learn the same two things. Otherwise
the Resume/rewind list is silently broken while the chat itself works — an
easy thing to ship by accident.

```python
# _SyncOpenCodeServer.__init__ gains:
self.api_v2 = False
self.password = None

# in the banner loop:
match = _SERVER_URL_RE.search(line)
if match and not self.url:
    self.url = match.group(1).rstrip("/")
    self.api_v2 = _is_v2_banner(line)
    password_deadline = time.monotonic() + 2.0
    if not self.api_v2:
        return self          # v1 never prints a password; do not wait
if self.url and not self.password:
    secret = _SERVER_PASSWORD_RE.search(line)
    if secret:
        self.password = secret.group(1)
        return self
    if password_deadline and time.monotonic() >= password_deadline:
        return self
```

and thread `api_v2` / `password` through `_sync_request()` into every call
site.

---

## 7. Installing the patch over Package Control

TermMate ships as a single archive, `TermMate.sublime-package`, in
`Installed Packages\`. An unpacked directory at `Packages\TermMate\` does **not**
win while the archive is present. Move the archive out of the load path:

```powershell
Move-Item "$env:APPDATA\Sublime Text\Installed Packages\TermMate.sublime-package" `
          "$env:TEMP\TermMate.sublime-package.disabled"
```

macOS/Linux equivalents:

```bash
mv ~/Library/Application\ Support/Sublime\ Text/Installed\ Packages/TermMate.sublime-package \
   ~/TermMate.sublime-package.disabled
```

Back up the single file before editing:

```powershell
Copy-Item "$env:APPDATA\Sublime Text\Packages\TermMate\genfoundry\opencode_agent.py" `
          "$env:TEMP\opencode_agent.py.orig"
```

**Restart Sublime Text after moving the archive** — otherwise the already-loaded
plugin host keeps the old module and you will debug a fix that is not running.

> This arrangement is temporary by design. Any Package Control update restores
> the archive and reverts the patch. Keep the `.orig` backup and be prepared to
> re-apply or upstream the diff.

---

## 8. Verification

Do not trust a syntax check. `py_compile` passing means nothing about whether
SSE events flow.

**The adapter has no `sublime` import** — `opencode_agent.py` and
`base_agent.py` are pure stdlib — so it can be exercised outside Sublime. Copy
both into a temp package dir with an empty `__init__.py`, import, and drive it
directly. (Importing through `genfoundry/__init__.py` pulls in the other agents,
which *do* import `sublime` — load the two files directly instead.)

```python
options = AgentOptions(
    cwd=PROJECT, cli_path=CLI,
    allowed_tools=[], disallowed_tools=[],
    permission_mode="default", approve_mode="default",
    model="<provider>/<model>",
)
agent = OpenCodeAgent(options)
await agent.connect()
# then drain receive_messages(), auto-answering control_request with {"behavior": "allow"}
```

Assert on the full round trip, not just connection:

```
api_v2 = True   server_url = http://127.0.0.1:49378   password = yes   model count = 17
PERMISSION: Bash {"command": "echo TERMMATE_V2_OK", "permission_type": "shell"}
tool: {"name": "command_execution", "command": "echo TERMMATE_V2_OK",
       "output": "TERMMATE_V2_OK\r\n", "status": "completed"}
text: 'TERMMATE_V2_OK'   errors=0   interrupt ok   rewind ok
RESULT: PASS
```

Cover all of these, because each exercises a different patched path:

- model list is non-empty (proves banner, auth, `/api` prefix, retry loop)
- a **shell tool call survives the approval round trip** (proves `permission.asked`
  → reply → multi-event tool accumulation → text delta → `execution.succeeded`)
- `interrupt()` returns cleanly (no hang, no orphaned server)
- `rewind()` returns without raising
- the spawned server process is dead afterwards (check for orphans)

A prompt like *"Use the shell tool to run exactly: `echo TERMMATE_V2_OK`. Then
reply with the output and nothing else."* forces every path in one turn.

Also verify your configuration is not clobbered: with the adapter's
`OPENCODE_CONFIG_CONTENT` set, confirm `opencode debug config` still lists the
user's `opencode.json` in full.

---

## 9. Known gaps after this patch

Honest accounting — these were deliberately **not** fixed:

1. **Artifacts / changed-files panel will likely stay empty on v2.** v1 learned
   about edits from a `session.diff` SSE event. v2 exposes only a
   `GET /api/session/{id}/diff` route, and no diff event appears on the stream.
   Edits apply correctly; they just are not listed. A poll against that route
   after `session.execution.succeeded` would fix it.
2. **Rewind has no message granularity on v2** — it truncates to session start.
3. **Reasoning-effort variants are not surfaced.** v2 `Model.Info` carries
   `variants` (`low`/`medium`/`high`/`xhigh`/`max`) that TermMate's picker does
   not enumerate.

---

## 10. Gotchas checklist

- [ ] Never trust a 200 from an unprobed path — v2 returns SPA HTML for unknown routes.
- [ ] Never `Stop-Process -Name opencode`; it may kill your own agent session.
- [ ] Long-lived servers: `background=true` + file redirect, or a self-contained Python driver.
- [ ] PowerShell: use `.cmd` shims, not `.ps1`; quote paths containing spaces.
- [ ] Probe `/api/model` warm **and** cold before concluding the patch is broken.
- [ ] Check the model picker with a chat session actually open — an empty picker
      with no connected session is expected behaviour, not evidence of a bug.
- [ ] Confirm Sublime really loaded the patched directory (archive moved aside, ST restarted).
- [ ] Set `"log_level": "DEBUG"` in TermMate settings and watch the ST console
      (`Ctrl+\``) when a failure is not reproducible outside ST.

---

## 11. Version context

| Package | Line | Latest at time of writing |
|---|---|---|
| `@opencode/cli` | **v2** (Go binary; what `opencode.ai/install` serves) | 2.0.x, stable since 2026-09-12 |
| `opencode-ai` | **v1** (the Python-era CLI TermMate targets) | 1.18.x, still maintained |
| `TermMate` | 0.5.8, released 2026-09-16 | ships the v1-only adapter |

v1 and v2 are **separate npm packages**, not a handoff. TermMate 0.5.8 shipped
four days after v2.0.0 went stable without adapting, so the breakage affects
every fresh install, not just an unlucky upgrade. Check whether upstream has
shipped support before repeating this work.

---

## 12. Rollback

```powershell
Move-Item "$env:TEMP\TermMate.sublime-package.disabled" `
          "$env:APPDATA\Sublime Text\Installed Packages\TermMate.sublime-package"
Remove-Item "$env:APPDATA\Sublime Text\Packages\TermMate" -Recurse -Force
```

Restart Sublime Text. Or, if the unpacked directory is already in place, just
restore the one file:

```powershell
Copy-Item "$env:TEMP\opencode_agent.py.orig" `
          "$env:APPDATA\Sublime Text\Packages\TermMate\genfoundry\opencode_agent.py" -Force
```