# Patching TermMate (Sublime Text 4) for OpenCode v2

A reproducible guide for AI agents and humans who need to make the
[TermMate](https://termmate.app) Sublime Text 4 package work with the OpenCode
v2 CLI. Written from a real Windows repair; the analysis and the patch are
platform-agnostic, and platform-specific differences are called out inline.

**Status:** verified working end-to-end against `opencode v2.0.27`.
**Scope:** `genfoundry/opencode_agent.py` only. No other TermMate file needed edits.

> **Revision note (2nd pass, opencode v2.0.27).** The first version of this guide
> shipped several claims that do not hold against v2.0.27 — most importantly a
> wrong baseline symptom (§1), the wrong model identifier (§6.5), a wrong claim
> that `/prompt` blocks (§6.7), and a suggested artifact fix that silently cannot
> work (§9.1). It also omitted working code for the v2 permission prompt and the
> v2 message envelope. Every correction is marked **[CORRECTED]** in place and
> summarised in [§13](#13-correction-log-second-pass).

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
- Opening an OpenCode chat fails after ~10 seconds with a bare
  "TimeoutError" / "OpenCode failed to start"
- The agent label in the picker still reads "opencode: (OpenCode by ...)"
```

Why the label still appears: the label comes from a **separate `--version`
probe** that only checks that the CLI binary is on disk. It never touches the
server. So "the agent is detected" tells you nothing about whether the
integration works.

**[CORRECTED]** Earlier revisions of this guide claimed the symptom was "it
connects but shows nothing — the model picker is empty and no content is ever
produced". **That is wrong, and it misdirects debugging.** The very first
breakage is fatal and immediate:

`_start_server()` wraps `_read_server_url()` in
`await asyncio.wait_for(..., timeout=10.0)`. The v1 regex
`opencode server listening on` **never matches** the v2 banner
`server listening on`, so the read loop never returns and the `wait_for`
fires. Measured against v2.0.27 with the unpatched adapter:

```
connect() FAILED after 10.0s
  TimeoutError:
```

So the failure is a **connect timeout with an empty message**, not a silent
empty chat. If you go looking for a model-list or event-stream bug first, you
will waste the whole session. Fix the banner regex and the other three
breakages become visible — expect a cascade.

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

| 5 | Model identity | `modelID` | **`id`** — 39 of 128 models have `id != modelID` (see §5) |
| 6 | Provider list | `/provider` → `{connected, all}` | `/api/provider` → **bare list, empty** even with credentials |

The nastiest trap in the whole exercise:

> **Unprefixed paths on v2 return `200 OK` with the SPA HTML, not a 404.**

An unknown route silently returns the web UI's `index.html` (7164 bytes,
`text/html`). A naive client sees "success" and parses garbage. When probing,
always assert on **content type and shape**, never on status code alone. This is
why `/api/provider` "worked" in early manual probing while returning the
homepage.

Measured on v2.0.27:

```
GET /api/info   -> 200 application/json  version=2.0.27
GET /provider   -> 200 text/html         7202 bytes   <- the SPA, not an API
```

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
4. **Probe cold and warm.** v2 resolves provider credentials asynchronously, so
   in principle `/api/model` can return `data: []` for the first moments.
   **[CORRECTED]** This was **not reproducible on v2.0.27** — a freshly started
   server returned all 128 models on the *first* request at `t=+0.38s`. Keep a
   retry loop as cheap insurance, but do not expect it to be load-bearing, and
   do not treat "picker is empty on the first try" as your smoking gun.

5. **Find a model that actually answers, before you write any code.** This is
   the step most likely to waste your morning. Facts measured on v2.0.27:

   | Source | Result |
   |---|---|
   | `GET /api/provider` | `{data: [], location}` — a **bare list, always empty**, even with a valid credential. Useless for provider discovery. |
   | `GET /api/model` | 128 entries, `enabled: true`, across `openai`/`google`/`opencode`/`ollama`/`deepseek` |
   | `GET /api/credential` | lists which integrations are actually credentialed |

   A model appearing in `/api/model` does **not** mean it will answer:
   - `opencode/cat-jam-free` → `session.execution.failed`, `403 Endpoint is unavailable` in ~1s
   - `opencode/space-bunny-free` → **stalls indefinitely**, emits nothing after `session.inbox.delivered`
   - `ollama/*` → instant fail if no local Ollama daemon is running
   - the model in `GET /api/model/default` is not necessarily healthy

   Pick a model whose provider shows up in `/api/credential`. On a machine
   authenticated only against DeepSeek, `deepseek/deepseek-flash` was the only
   candidate that completed a full turn.

   > **Caution:** `/api/credential` returns stored provider API keys **in
   > plaintext**. Do not paste its output into a ticket, a chat log, or a
   > committed file.

---

## 4. Environment constraints you will hit

These cost real time during the original repair; record them so the next agent
does not rediscover them.

| Constraint | Detail |
|---|---|
| **Long-lived servers kill the agent shell** | `opencode serve` never exits. `Start-Process -NoNewWindow`, `Start-Job`, and foreground invocation all abort the tool call, because the harness waits on descendant processes and inherited handles. |
| **Solution** | Launch with the shell tool's `background=true` and redirect output to a file (`1> file 2>&1`), then read the file in a *separate* foreground command. For anything more complex, write a Python script that spawns, probes, and kills the server inside one foreground invocation. |
| **Never `Stop-Process -Name opencode`** | If the agent harness itself runs on OpenCode, this kills your own session. Target a specific PID captured at spawn time, or never kill by name. |
| **`Popen(["opencode.cmd", ...]).terminate()` leaks the real server** | **[NEW]** `opencode.cmd` is a batch shim. Terminating it kills the *wrapper*, not the `opencode.exe` child, which survives holding its port. Standalone probe scripts must clean up with `taskkill /PID <pid> /T /F`. The patched adapter is immune because it puts the server in a Windows Job Object (`KILL_ON_JOB_CLOSE`) — which is also why the §8 orphan check passes for it. On a single verification run this leaked **11** orphaned `opencode.exe` servers. |
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
| Providers | same route as models | `GET /api/provider` → **`{data: []}`** — a bare list, empty even with credentials. Do not use it for discovery. |
| Credentials | — | `GET /api/credential` → plaintext keys; use only to find a working provider |
| Sessions | `GET /session` | `GET /api/session` |
| Create session | `POST /session` | `POST /api/session` |
| Send turn | `POST /session/{id}/prompt_async` body `{parts, agent, model, system, tools}` | `POST /api/session/{id}/prompt` body **`{text}`** — **no `system` field**; `prompt_async` is 404. **[CORRECTED]** It does **not** block for the turn on v2.0.27 (measured: returns in **0.11s** carrying the user message). Earlier revisions claimed it blocks; SSE carries the turn either way. |
| Select model | part of the prompt body | `POST /api/session/{id}/model` body `{"model": {id, providerID, variant?}}` → 204. `Model.Ref` uses **`id`**, not `modelID` — see below |
| Select agent | `agent` field in prompt body | `POST /api/session/{id}/agent` body `{"agent": "<name>"}`. Ids are lowercase (`build`, `plan`); `/api/agent` lists them capitalised (`Build`, `Plan`) |
| Interrupt | `POST /session/{id}/abort` (404 on v2) | `POST /api/session/{id}/interrupt` → `{"interrupted": false}` |
| Revert | `POST /session/{id}/revert` (404 on v2) | `DELETE /api/session/{id}/revert` — **no message selector**, truncates to session start |
| Permissions | `POST /session/{id}/permissions/{pid}` | `POST /api/session/{id}/permission/{requestID}/reply` body `{"decision":"once"\|"always"\|"reject"}` → 204. Note the **singular** `permission` segment and the required `{sessionID}` prefix |
| Events | `GET /event` | `GET /api/event` |
| Session diff | `session.diff` event | `GET /api/session/{id}/diff` — returns `{data: []}` in practice; see §9.1 |

**`id` vs `modelID` — the subtlest bug in this port.** `Model.Info` exposes both,
and for **39 of 128** entries they differ, because `id` is the concrete address
and `modelID` is the family:

```
id='gpt-6.1-sol'        modelID='gpt-6.1-sol'
id='gpt-6.1-sol-fast'   modelID='gpt-6.1-sol'
id='gpt-6.1-sol-pro'    modelID='gpt-6.1-sol'
id='gpt-6.1-sol-ultrafast' modelID='gpt-6.1-sol'
```

Since `Model.Ref` is keyed on `id`, building the picker value from `modelID`
silently selects the *base* model when the user picked a variant. Build picker
values as `providerID/id`. Keeping the `providerID/modelID` shape for backward
compatibility is harmless — `id == modelID` for the other 89 entries — but it
must not be the *source* of the id.

Enums: `Permission.Reply = ["once","always","reject"]`,
`Permission.Effect = ["allow","deny","ask"]`.
`Model.Info` fields: `id, modelID, providerID, name, capabilities, variants, time, cost, status, enabled, limit`.
`FileDiff.Info` fields: `file, patch, additions, deletions, status` — **not** v1's
`{file, before, after}`, which is why §9.1's poll cannot be dropped in as-is.

**Request bodies are closed.** `/api/session/{id}/prompt`, `.../model`,
`.../agent` and `.../permission/{id}/reply` all declare
`"additionalProperties": false`. Passing the v1 body (`parts`, `system`,
`tools`, `reply`) is a hard 4xx, not a silently ignored field.

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
| `session.created` | `{sessionID, slug, version, projectID, location, subpath}` | ignore |
| `session.renamed` | `{sessionID, title}` | session title |
| `session.usage.updated` | `{sessionID, cost, tokens:{input,output,reasoning,cache:{read,write}}}` | usage display |
| `session.step.started` | `{sessionID, agent, model:{id,providerID,variant}, assistantMessageID, started}` | turn start |
| `session.text.started` / `.delta` / `.ended` | `{sessionID, assistantMessageID, ordinal, delta|text}` | **text streaming** (`.delta` is the hot path) |
| `session.reasoning.started` / `.delta` / `.ended` | `{sessionID, assistantMessageID, ordinal, delta\|text, state}` | **[NEW]** thinking block — emit `MessageType.THINKING`, keyed on `assistantMessageID`. Omitting this silently loses all reasoning output |
| `session.tool.input.started` | `{sessionID, assistantMessageID, id:"call_…", name}` | tool start; terminal tool is named **`shell`** |
| `session.tool.input.ended` | `{…, id, text}` | raw input JSON; ignore (`.called` has it parsed) |
| `session.tool.called` | `{…, id, input:{command:…}, executed:false}` | **input available here** |
| `session.tool.progress` | `{…, id, metadata:{shellID}}` | running |
| `session.tool.success` | `{…, id, content:[{type:"text",text}], metadata:{status,truncated,exit}, executed}` | tool complete |
| `session.step.streamed` / `.ended` | `{finish:"stop"\|"tool-calls", rawFinish, cost, tokens}` | step boundary |
| `session.step.failed` | `{sessionID, assistantMessageID, error}` | step-level failure; the turn-level event below also fires |
| `session.execution.started` | `{sessionID}` | turn start (ignore) |
| `session.execution.succeeded` | `{sessionID}` | **turn end — emit STOP** |
| `session.execution.failed` | `{sessionID, error:{type, message, status, response:{body}}}` | **turn end — emit ERROR** |
| `session.inbox.enqueued` | `{sessionID, inboxID, item:{type,payload:{text}}}` | **[NEW]** the user prompt being accepted — `inboxID` is the v2 stand-in for v1's `message.updated` user-message id. Emit `user_message_id` from here or rewind loses its anchor |
| `session.inbox.delivered` | `{sessionID, inboxID}` | ignore |
| `session.instructions.updated` | `{sessionID, delta:{...}}` | ignore |
| `session.retry.scheduled` | `{sessionID, assistantMessageID, attempt, at, error}` | **[NEW]** see the warning below |
| `permission.asked` | `{id:"per_…", sessionID, action:"shell", resources:[…], save:[…], source:{type,messageID,id}}` | approval prompt |
| `permission.replied` | `{sessionID, requestID, reply}` | approval echo |
| `shell.created` / `shell.exited` | `{info:{id,status,command,cwd,…}}` / `{id, exit, status}` | PTY lifecycle |
| `project.updated`, `model.updated`, `provider.updated`, `agent.updated`, `command.updated`, `skill.updated`, `websearch.updated`, `reference.updated`, `plugin.updated`, `integration.updated` | `{}` | server bootstrap chatter; ignore |

> **`session.retry.scheduled` and the hang trap.** When a provider cannot be
> reached, the server retries **indefinitely** (`attempt: 2, 3, 4, …`, roughly
> every 25s) and never emits `session.execution.failed`. The adapter therefore
> correctly keeps waiting — but the user sees a spinner forever. Do not "fix"
> this in the adapter by inventing a timeout; it is server behaviour and the
> correct user action is Stop, which the patched `interrupt()` handles (verify
> it clears `_turn_active`). A *terminal* failure looks different and short:
> `opencode/cat-jam-free` 403s in ~1s and does emit `execution.failed`.

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

`value` must be `f"{provider_id}/{model['id']}"`, **not** `modelID` —
**[CORRECTED]**. The first revision of this guide specified `model_id` here and
then posted `{"id": model["modelID"]}` in §6.7. Both are wrong for the 39
aliased entries in §5 (`gpt-6.1-sol-fast` and friends), which would silently
downgrade the user's pick to the base model. The `providerID/...` shape itself
is unchanged, so TermMate settings and the status bar keep working, and a
previously saved `providerID/modelID` value still resolves — it just resolves to
the base model, which is what that value meant anyway.

```python
provider_id = model.get("providerID")
# `id` is the addressable model; `modelID` is the family it belongs to.
model_ref = model.get("id") or model.get("modelID")
if not provider_id or not model_ref:
    continue
models.append({
    "displayName": model.get("name") or model_ref,
    "description": provider_id,
    "value": f"{provider_id}/{model_ref}",
})
```

De-duplicate on the resulting `value`. On v2.0.27 the 128 raw entries collapse
to 128 distinct values (89 unique `provider/model` families, the remainder
being the variants), and all report `enabled: true`.

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

Declare `self._pending_tools: Dict[str, Dict[str, Any]] = {}` in `__init__`
next to `_terminal_tools`, and clear it in `send_message()` (per turn),
`interrupt()`, `new_session()` and `rewind()` — otherwise a call id that never
reaches a terminal event leaks state across turns.

#### The v2 permission prompt **[NEW — this is the part the first revision omitted]**

`permission.asked` shares almost nothing with v1's `_handle_permission`:

| | v1 | v2 |
|---|---|---|
| kind | `type` / `permission` | **`action`** (e.g. `"shell"`, `"edit"`) |
| target | `metadata.command`, `patterns` | **`resources`**, **`save`** |
| link to call | none | **`source.id`** → the `call_…` id |

Reusing the v1 handler yields `permission_type: "tool"` with **no command at
all**, which makes the §8 expectation
(`PERMISSION: Bash {"command": "echo TERMMATE_V2_OK", "permission_type": "shell"}`)
unreachable. `permission.asked` arrives *after* `session.tool.called`, so the
command is already sitting in `_pending_tools`, keyed by `source.id`:

```python
async def _handle_v2_permission(self, data: Dict[str, Any]) -> None:
    permission_id = str(data.get("id") or "")
    if not permission_id or permission_id in self._seen_permissions:
        return
    self._seen_permissions.add(permission_id)

    session_id = str(data.get("sessionID") or self._session_id or "")
    self._permission_sessions[permission_id] = session_id

    action = str(data.get("action") or "tool")
    tool_name = _TOOL_NAMES.get(action.lower(), action)

    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    pending = self._pending_tools.get(str(source.get("id") or "")) or {}
    tool_input = pending.get("input")

    resources = data.get("resources")
    if isinstance(tool_input, dict) and tool_input:
        input_data: Dict[str, Any] = dict(tool_input)
    elif isinstance(resources, list) and resources:
        input_data = {
            "command": resources[0] if len(resources) == 1 else list(resources)
        }
    else:
        input_data = {}

    input_data.update({
        "resources": resources,
        "save": data.get("save"),
        "title": source.get("type") or "",
        "permission_type": action,
    })

    await self._message_queue.put(Message(
        "control_request",
        content={
            "request_id": permission_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool_name,
                "input": input_data,
            },
        },
    ))
```

Also emit the user-prompt acknowledgement, which v1 got from
`message.updated` and which the chat view uses as its rewind anchor:

```python
if event_type == "session.inbox.enqueued":
    inbox_id = str(data.get("inboxID") or "")
    if inbox_id and self._awaiting_user_message_id:
        self._awaiting_user_message_id = False
        await self._message_queue.put(Message(
            "user_message_id",
            content={"message_id": inbox_id},
            msg_id=inbox_id,
        ))
    return
```

Finally, map reasoning to the thinking block:

```python
if event_type == "session.reasoning.delta":
    delta = str(data.get("delta") or "")
    if delta:
        await self._message_queue.put(Message(
            MessageType.THINKING.value,
            content=delta,
            msg_id=data.get("assistantMessageID"),
        ))
    return
```

### 6.7 Turn submission

**[CORRECTED]** The first revision stated that
`POST /api/session/{id}/prompt` "blocks for the entire turn" and that awaiting
it "deadlocks against the event stream". On v2.0.27 it does **not** block — it
returns in ~0.11s with the user message and the turn proceeds over SSE.

Fire-and-forget is still the right call, but for a different and more robust
reason: a transport that *did* block would blow past `_http`'s default 30s
timeout and raise a spurious error mid-turn. So keep the background task **and
raise the timeout**:

```python
if self._api_v2:
    await self._apply_v2_session_settings(session_id, agent_name)
    asyncio.create_task(self._prompt_v2(session_id, content))
else:
    await self._http("POST", f"/session/{sid}/prompt_async", body=body)
```

```python
async def _prompt_v2(self, session_id: str, content: str) -> None:
    try:
        await self._http(
            "POST", f"/session/{quote(session_id, safe='')}/prompt",
            body={"text": content},
            timeout=300.0,          # v2 returns immediately, but do not assume it
        )
    except Exception as error:
        if self._turn_active:
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            await self._message_queue.put(Message(MessageType.ERROR.value, content=str(error)))
```

```python
async def _apply_v2_session_settings(self, session_id, agent_name):
    try:
        await self._http("POST", f"/session/{session_id}/agent", body={"agent": agent_name})
    except Exception as error:
        LOG.debug("OpenCode agent selection failed: %s", error)
    model = self._model_payload()
    if model:
        try:
            # v2 Model.Ref uses "id", not "modelID" -- see 6.5
            await self._http("POST", f"/session/{session_id}/model",
                             body={"model": {"id": model["id"], "providerID": model["providerID"]}})
        except Exception as error:
            LOG.debug("OpenCode model selection failed: %s", error)
```

Both failures here are non-fatal — log at debug and continue.

`_prompt_v2` must close the turn on transport failure, or `send_message()`
deadlocks forever waiting on `_turn_done` (see the implementation above).

### 6.8 Permission replies

```python
if self._api_v2:
    body = {"decision": response}          # "once" | "reject"
    if response == "reject" and response_data.get("message"):
        body["message"] = response_data["message"]
    await self._http(
        "POST",
        "/session/{}/permission/{}/reply".format(
            quote(session_id, safe=""), quote(str(permission_id), safe="")
        ),
        body=body,
    )
    return
```

**[NEW]** The `{sessionID}` prefix is easy to drop, and the failure mode is a
confusing 404 in the middle of an otherwise healthy turn:

```
OpenCodeHTTPError: OpenCode POST http://127.0.0.1:PORT/api/permission/per_…/reply
returned HTTP 404
```

The path is `/api/session/{sid}/permission/{pid}/reply` — singular
`permission`, and **not** `/api/permission/…`. Use `session_id` from the
`self._permission_sessions.pop(...)` lookup above, which is already in scope.

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
site:

```python
def _sync_request(base_url, path, cwd, extra_env, api_v2=False, password=None):
    if api_v2 and not path.startswith("/api"):
        path = "/api" + path
    ...
    headers={"Accept": "application/json",
             **_sync_auth_headers(extra_env, password)}
```

#### `get_opencode_session_info` also needs a shape fix **[NEW]**

Threading the flags is **necessary but not sufficient**. `GET
/api/session/{id}/message` returns a completely different envelope on v2, so
`get_opencode_session_info()` silently returns **zero turns** and the Resume
panel comes up blank even though the flag is threaded correctly.

| | v1 | v2 |
|---|---|---|
| entry | `{info: {role, ...}, parts: [{type:"text", text}]}` | **flat**: `{id, type, text?, content: [{type:"text", text}], ...}` |
| paging | none | envelope adds `cursor` (handled by `_unwrap`) |
| envelope | bare list | `{data: [...], cursor: {...}}` |

v2 also interleaves **pseudo-messages** that are not turns at all — `type`
values `"idle"`, `"model-switched"` and `"agent-switched"` — so filter on `type`
rather than assuming every entry is user/assistant:

```python
for entry in entries:
    if not isinstance(entry, dict):
        continue
    if server.api_v2:
        role = entry.get("type")
        if role == "user":
            text = str(entry.get("text") or "").strip()
        else:
            text = "".join(
                part.get("text", "")
                for part in entry.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
    else:
        info = entry.get("info") or {}
        role = info.get("role")
        text = "".join(
            part.get("text", "")
            for part in entry.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "text"
            and not part.get("ignored")
        ).strip()
    if role == "user" and text:
        ...
    elif role == "assistant" and text and current is not None:
        ...
```

`list_opencode_sessions()` needs **no** shape change — v2 sessions still expose
`id`, `title` and `time.updated` — only the prefix and auth threading above.

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

Assert on the full round trip, not just connection. Cover all of these,
because each exercises a different patched path:

- model list is non-empty (proves banner, auth, `/api` prefix, retry loop)
- a **shell tool call survives the approval round trip** (proves `permission.asked`
  → reply → multi-event tool accumulation → text delta → `execution.succeeded`)
- the permission prompt carries the real command and `permission_type: "shell"`
  (only passes if `_handle_v2_permission` was written — see §6.6)
- `interrupt()` returns cleanly (no hang, no orphaned server)
- `rewind()` returns without raising
- **the synchronous picker**: `list_opencode_sessions()` is non-empty and
  `get_opencode_session_info()` recovers at least one turn (proves §6.10's
  prefix, auth **and** message-shape fixes)
- **the failure path**: a model that 403s produces a `MessageType.ERROR` and
  clears `_turn_active`, and a *second* prompt does not deadlock
- the spawned server process is dead afterwards (check for orphans)

A prompt like *"Use the shell tool to run exactly: `echo TERMMATE_V2_OK`. Then
reply with the output and nothing else."* forces every path in one turn.

**Pick a working model first** (§3.5), and pass it in as
`AgentOptions(model=...)`. Otherwise a broken model looks like a broken patch.

Two measurement traps in the harness itself:

- **Capture the PID before `disconnect()`.** `disconnect()` sets
  `_server_process = None`, so checking afterwards reports `pid=None` and the
  orphan assertion passes vacuously.
- **An unreachable provider retries forever** and never emits a terminal
  event, so a "failure path" test using one will hang and look like an adapter
  bug. Use `opencode/cat-jam-free` (fails in ~1s) instead.

Observed run of the passing configuration:

```
api_v2 = True   server_url = http://127.0.0.1:49263   password = True   models = 128
PERMISSION: Bash {"command": "echo TERMMATE_V2_OK", "permission_type": "shell"}
tool: {"name":"command_execution","command":"echo TERMMATE_V2_OK",
       "output":"TERMMATE_V2_OK\r\n","status":"completed"}
text: 'TERMMATE_V2_OK'   errors=0   interrupt ok   rewind ok   server process dead
RESULT: PASS
```

Also verify your configuration is not clobbered: with the adapter's
`OPENCODE_CONFIG_CONTENT` set, confirm `opencode debug config` still lists the
user's `opencode.json` in full. On v2.0.27 `debug config` returns a list of
config *sources* (`{"type":"document","path":...}`), and the `permission`
overlay merges into it rather than replacing the file.

---

## 9. Known gaps after this patch

Honest accounting — these were deliberately **not** fixed:

1. **Artifacts / changed-files panel stays empty on v2 — and the obvious fix
   does not work.** **[CORRECTED]** The first revision said a poll against
   `GET /api/session/{id}/diff` after `session.execution.succeeded` "would fix
   it". Implemented and measured against v2.0.27: the route returns
   **`{data: []}`** even immediately after a turn that verifiably created a
   file (retried at 0s/2s/5s), and `/api/vcs/status` likewise reports nothing
   outside a git checkout. Its payload is also `FileDiff.Info`
   `{file, patch, additions, deletions, status}` — **not** the v1
   `{file, before, after}` that `_handle_session_diff()` expects, so the data
   would have to be reassembled from the unified `patch` as well. Edits apply
   correctly; they are just not listed. Do not ship the poll expecting it to
   fire.
2. **Rewind has no message granularity on v2** — it truncates to session start.
3. **Reasoning-effort variants are not surfaced.** v2 `Model.Info` carries
   `variants` (`low`/`medium`/`high`/`xhigh`/`max`) that TermMate's picker does
   not enumerate. Note the *separate* `-fast`/`-pro`/`-ultrafast` model ids from
   §5, which the patch **does** surface correctly.
4. **`/api/provider` yields no provider metadata** on v2, so TermMate's
   v1-derived "is this provider connected" filtering has no v2 equivalent. The
   picker relies on `/api/model`'s `enabled` flag instead, which reports `true`
   for all 128 entries even when the provider cannot actually be reached — so a
   broken provider is selectable and only fails at turn time.
5. **Unreachable providers hang the turn indefinitely** (see the
   `session.retry.scheduled` note in §5). Stop is the only escape.

---

## 10. Gotchas checklist

- [ ] Never trust a 200 from an unprobed path — v2 returns SPA HTML for unknown routes.
- [ ] **Check the unpatched baseline first.** Run the original adapter against v2
      and record the real symptom; it is a 10s connect `TimeoutError`, not an
      empty chat. Do not go hunting for a model-list bug first.
- [ ] Use `id`, not `modelID`, when addressing a model (`Model.Ref`, picker values).
- [ ] The permission reply needs `/session/{sid}/permission/{pid}/reply` — the
      `{sid}` prefix is not optional.
- [ ] Never `Stop-Process -Name opencode`; it may kill your own agent session.
- [ ] `Popen(["opencode.cmd"]).terminate()` leaks `opencode.exe`; use
      `taskkill /PID <pid> /T /F` in throwaway probe scripts.
- [ ] Confirm you have a **working** model before judging the patch (§3.5).
      `opencode/*-free` models 403 or stall; check `/api/credential`.
- [ ] Long-lived servers: `background=true` + file redirect, or a self-contained Python driver.
- [ ] PowerShell: use `.cmd` shims, not `.ps1`; quote paths containing spaces.
- [ ] Probe `/api/model` warm **and** cold before concluding the patch is broken.
- [ ] Check the model picker with a chat session actually open — an empty picker
      with no connected session is expected behaviour, not evidence of a bug.
- [ ] Confirm Sublime really loaded the patched directory (archive moved aside, ST restarted).
- [ ] Set `"log_level": "DEBUG"` in TermMate settings and watch the ST console
      (`Ctrl+\``) when a failure is not reproducible outside ST.
- [ ] Capture the server PID **before** `disconnect()` when asserting no orphans.

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

**Upstream status, re-checked 2026-10-13** (against `flashmodel/termmate`):
`main:genfoundry/opencode_agent.py` is still **byte-identical** to the shipped
0.5.8 archive (67 773 bytes) and contains no `_api_v2`, no `/api` prefixing and
no `session.text.delta`. **The port is still required**, and any Package Control
update will restore the archive and silently revert the patch. Verify with:

```powershell
$raw = "https://raw.githubusercontent.com/flashmodel/termmate/main/genfoundry/opencode_agent.py"
# upstream file size vs the archive's; look for "_api_v2" / "prompt_async"
```

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

---

## 13. Correction log (second pass)

Recorded from re-applying this guide end-to-end against **opencode v2.0.27** on
Windows. Each row was verified, not inferred: the baseline symptom and the
model-id bug were reproduced with the unpatched adapter and against
`/openapi.json`, and the `/prompt` and cold-fetch claims were re-measured.

### 13.1 Factual errors in the first revision

| # | Section | Claimed | Actual on v2.0.27 |
|---|---|---|---|
| E1 | §1 | "it connects but shows nothing; the model picker is empty" | `connect()` **fails** after exactly 10.0s with a bare `TimeoutError` — the v1 banner regex never matches, so `_start_server`'s `wait_for` fires. Wrong mental model sends you hunting the wrong layer |
| E2 | §6.5, §6.7 | picker `value` = `providerID/modelID`; post `{"id": model["modelID"]}` | **`id` ≠ `modelID` for 39 of 128 models** (`-fast`/`-pro`/`-ultrafast`). `Model.Ref` is keyed on `id`, so the guide's code silently downgrades the user's pick |
| E3 | §6.7 | `/prompt` "blocks for the whole turn; awaiting it deadlocks" | returns in **0.11s**. Advice is still safe, but the stated rationale is wrong and hides the real reason to raise the timeout |
| E4 | §3.4, §6.5 | "cold `/api/model` returns `data: []` for the first few seconds" | **not reproduced** — 128 models on the first request at `t=+0.38s`. Keep the retry loop, but it is not load-bearing |
| E5 | §9.1 | "a poll against `/api/session/{id}/diff` would fix artifacts" | route returns `{data: []}` after a turn that verifiably wrote a file (0s/2s/5s); payload is `FileDiff.Info{file,patch,…}`, incompatible with v1's `{file,before,after}`. The suggested fix cannot work |
| E6 | §5 | event table omitted `session.reasoning.*` | reasoning deltas exist and are the **only** thinking source; omitting them loses all thinking output |
| E7 | §5 | `/api/provider` undocumented | returns a **bare, always-empty list** `{data: []}` even with valid credentials — unusable for discovery, unlike v1's `{connected, all}` |

### 13.2 Missing code

| # | Gap | Consequence if not filled |
|---|---|---|
| G1 | §6.6 invoked `_handle_v2_permission(data)` but never defined it | v2 uses `action`/`resources`/`save`/`source.id`, not v1's `type`/`metadata`. Reusing the v1 handler yields `permission_type: "tool"` with **no command** — and the guide's own §8 expectation becomes unreachable |
| G2 | `session.reasoning.delta` → `MessageType.THINKING` unspecified | no thinking output |
| G3 | §6.10 implied prefix+auth threading was sufficient for `get_opencode_session_info` | v2 messages are **flat** (`{id, type, text, content:[]}`) with pseudo-types `idle`/`model-switched`/`agent-switched`; turns come back **empty** and Resume looks broken while chat works |
| G4 | `_pending_tools` never declared or cleared | state leaks across turns on any call id that never reaches a terminal event |
| G5 | `user_message_id` parity unstated | v1 emitted it from `message.updated`; v2's equivalent is `session.inbox.enqueued` → `data.inboxID`. Without it rewind loses its anchor |
| G6 | `_unwrap` guard | the guide's `set(value).issubset({…})` also matches a bare `{"error": …}`; add `"data" in value` |

### 13.3 Environment gaps

- **`Popen(["opencode.cmd"]).terminate()` leaks `opencode.exe`** — the batch shim
  dies, the Go binary survives holding its port. 11 orphans accumulated in one
  verification run. Use `taskkill /PID <pid> /T /F`; the patched adapter is safe
  because of its Job Object.
- **Nothing tells you how to pick a working model.** `/api/provider` is empty,
  most `opencode/*-free` models 403 or stall, and `/api/credential` is the only
  reliable signal (it also leaks keys in plaintext — don't paste its output).
- **Verification harness traps:** capture the PID before `disconnect()` (it
  nulls `_server_process`, making the orphan check pass vacuously), and don't
  test the failure path with a provider the server retries forever.