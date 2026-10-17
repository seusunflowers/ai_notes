"""Apply the TermMate v2 patch (manual section 6) to the unpacked opencode_agent.py.

Idempotent: each edit replaces `old` with `new`; if `old` is absent but `new`
is already present the edit is reported as SKIP.  Writes nothing unless every
edit is either applied or already present.

Self-contained: the input may be the pristine upstream file straight out of
the unpacked archive (manual section 7).  Edit `6.1-banner-regexes` is
included, so the old "apply section 6.1 by hand first" prerequisite is gone;
running against a file that already has 6.1 applied is a safe SKIP.

Rewind (`rewind-v2`) targets the opencode 2.0.32 wire format: the staged
`POST .../revert/stage` + `POST .../revert/commit` pair, which preserves
message granularity.  On 2.0.32 `DELETE /revert` is a silent no-op, so it is
used only as a 404/405 fallback for older v2 builds (which truncate to the
start of the session instead).
"""
import os
import shutil
import sys
import time

APP_DATA = os.environ.get("APPDATA") or os.path.expanduser("~")
TEMP_ROOT = os.environ.get("TEMP") or os.path.expanduser("~")

# Defaults assume the unpacked TermMate package (manual section 7);
# override with TM_TARGET / TM_BACKUP on other machines.
TARGET = os.environ.get("TM_TARGET") or os.path.join(
    APP_DATA, "Sublime Text", "Packages", "TermMate", "genfoundry", "opencode_agent.py"
)
BACKUP = os.environ.get("TM_BACKUP") or os.path.join(
    TEMP_ROOT, "opencode", "opencode_agent.py.pre_script.bak"
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EDITS = [
    ("6.1-banner-regexes", r'''_SERVER_URL_RE = re.compile(
    r"opencode server listening on\s+(https?://[^\s]+)", re.IGNORECASE
)''', r'''# v1 announces itself with an "opencode" prefix; v2 omits it.
_SERVER_URL_RE = re.compile(
    r"(?:opencode\s+)?server listening on\s+(https?://[^\s]+)", re.IGNORECASE
)
_SERVER_PASSWORD_RE = re.compile(r"server password\s+(\S+)", re.IGNORECASE)
_V1_BANNER_RE = re.compile(r"\s*opencode\s+server listening on\b", re.IGNORECASE)


def _is_v2_banner(line: str) -> bool:
    return not _V1_BANNER_RE.match(line)'''),

    ("6.6-tool-names", r'''_TOOL_NAMES = {
    "bash": "Bash",
    "edit": "Edit",''', r'''_TOOL_NAMES = {
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",'''),

    ("init-api-fields", r'''        self.server_url: Optional[str] = None

        self._session_id: Optional[str] = self.options.session_id''', r'''        self.server_url: Optional[str] = None
        self._api_v2 = False
        self._server_password: Optional[str] = None

        self._session_id: Optional[str] = self.options.session_id'''),

    ("init-pending-tools", r'''        self._terminal_tools = set()
        self._permission_sessions: Dict[str, str] = {}''', r'''        self._terminal_tools = set()
        self._pending_tools: Dict[str, Dict[str, Any]] = {}
        self._permission_sessions: Dict[str, str] = {}'''),

    ("read-url-password", r'''            line = raw.decode("utf-8", errors="replace")
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match:
                return match.group(1)''', r'''            line = raw.decode("utf-8", errors="replace")
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match:
                if _is_v2_banner(line):
                    self._server_password = await self._read_server_password()
                return match.group(1)'''),

    ("read-server-password-method", r'''    async def _drain_server_output(self) -> None:''', r'''    async def _read_server_password(self, timeout: float = 3.0) -> Optional[str]:
        """Read the v2 password line that follows the banner URL."""
        process = self._server_process
        if not process or not process.stdout:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(), timeout=remaining
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return None
            except Exception:
                return None
            if not raw:
                return None
            match = _SERVER_PASSWORD_RE.search(
                raw.decode("utf-8", errors="replace")
            )
            if match:
                return match.group(1)

    async def _drain_server_output(self) -> None:'''),

    ("auth-precedence", r'''        source = env or {**os.environ, **self.options.extra_env}
        password = source.get("OPENCODE_SERVER_PASSWORD")''', r'''        source = env or {**os.environ, **self.options.extra_env}
        password = self._server_password or source.get("OPENCODE_SERVER_PASSWORD")'''),

    ("finish-connect-detect", r'''        await self._http("GET", "/global/health", include_directory=False)

        self._is_connected = True''', r'''        await self._detect_api_version()

        self._is_connected = True'''),

    ("detect-api-version-method", r'''    async def _start_server(self, env: Dict[str, str]) -> str:''', r'''    async def _detect_api_version(self) -> None:
        """Probe /api/info: it exists only on v2 servers."""
        self._api_v2 = False
        info = None
        try:
            info = await self._http("GET", "/api/info", include_directory=False)
        except Exception as error:
            LOG.debug("OpenCode /api/info probe failed: %s", error)
        if isinstance(info, dict) and info.get("version"):
            self._api_v2 = True
            LOG.info("Detected OpenCode v2 server (version %s)", info["version"])
            return
        await self._http("GET", "/global/health", include_directory=False)
        LOG.info("Detected OpenCode v1 server")

    async def _start_server(self, env: Dict[str, str]) -> str:'''),

    ("build-url-prefix", r'''        params = dict(query or {})
        if include_directory and self.options.cwd:''', r'''        if self._api_v2 and not path.startswith("/api"):
            path = "/api" + path

        params = dict(query or {})
        if include_directory and self.options.cwd:'''),

    ("unwrap-envelope", r'''        if isinstance(value, dict) and set(value).issubset({"data", "error"}):
            if value.get("error"):
                raise RuntimeError(OpenCodeAgent._error_text(value["error"]))
            return value.get("data")
        return value''', r'''        if (
            isinstance(value, dict)
            and "data" in value
            and set(value).issubset({"data", "error", "location", "cursor"})
        ):
            if value.get("error"):
                raise RuntimeError(OpenCodeAgent._error_text(value["error"]))
            return value.get("data")
        return value'''),

    ("dispatch-v2-branch", r'''    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        payload = self._event_payload(event)
        event_type = payload.get("type", "")''', r'''    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        payload = self._event_payload(event)
        if self._api_v2:
            await self._dispatch_event_v2(payload)
            return
        event_type = payload.get("type", "")'''),

    ("v2-dispatcher-methods", r'''    async def _finish_turn(self) -> None:''', r'''    async def _dispatch_event_v2(self, payload: Dict[str, Any]) -> None:
        event_type = payload.get("type", "")
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
        session_id = data.get("sessionID")
        if session_id and self._session_id and session_id != self._session_id:
            return

        if event_type == "session.text.delta":
            await self._emit_text_delta(
                str(data.get("delta") or ""), data.get("assistantMessageID")
            )
            return

        if event_type == "session.reasoning.delta":
            delta = str(data.get("delta") or "")
            if delta:
                await self._message_queue.put(Message(
                    MessageType.THINKING.value,
                    content=delta,
                    msg_id=data.get("assistantMessageID"),
                ))
            return

        if event_type == "session.tool.input.started":
            call_id = str(data.get("id") or "")
            if call_id:
                self._pending_tools[call_id] = {
                    "name": data.get("name") or "tool"
                }
            return

        if event_type == "session.tool.called":
            call_id = str(data.get("id") or "")
            if call_id:
                pending = self._pending_tools.setdefault(call_id, {})
                pending["input"] = data.get("input") or {}
            return

        if event_type in ("session.tool.success", "session.tool.error",
                          "session.tool.failed"):
            await self._finish_v2_tool(event_type, data)
            return

        if event_type == "permission.asked":
            await self._handle_v2_permission(data)
            return

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

        if event_type == "session.error":
            error_text = self._error_text(data.get("error") or data)
            LOG.error(
                "OpenCode conversation failed [session_id=%s, error=%r]",
                self._session_id,
                error_text,
            )
            await self._message_queue.put(Message(
                MessageType.ERROR.value, content=error_text
            ))
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            return

        if event_type == "session.retry.scheduled":
            LOG.warning(
                "OpenCode scheduled a retry [session_id=%s, data=%r]",
                self._session_id,
                data,
            )
            return

        if event_type in ("session.execution.succeeded",
                          "session.execution.interrupted"):
            if self._turn_active:
                LOG.info(
                    "OpenCode conversation succeeded [session_id=%s]",
                    self._session_id,
                )
            await self._finish_turn()
            return

        if event_type == "session.execution.failed":
            error_text = self._error_text(
                data.get("error") or data.get("reason") or data
            )
            await self._message_queue.put(Message(
                MessageType.ERROR.value, content=error_text
            ))
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()

    async def _finish_v2_tool(self, event_type: str, data: Dict[str, Any]) -> None:
        call_id = str(data.get("id") or "")
        if not call_id or call_id in self._terminal_tools:
            return
        self._terminal_tools.add(call_id)
        pending = self._pending_tools.pop(call_id, {})
        failed = event_type != "session.tool.success"
        chunks: List[str] = []
        content = data.get("content")
        if isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    chunks.append(str(chunk.get("text") or ""))
        state = {
            "status": "error" if failed else "completed",
            "input": pending.get("input") or {},
            "output": "".join(chunks),
            "error": self._error_text(data.get("error")) if failed and data.get("error") else None,
            "metadata": data.get("metadata") or {},
        }
        await self._emit_completed_tool(
            {"id": call_id, "tool": pending.get("name") or "tool"}, state
        )

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

    async def _finish_turn(self) -> None:'''),

    ("approval-reply-v2", r'''        behavior = response_data.get("behavior", "deny")
        response = "once" if behavior == "allow" else "reject"
        body = {"reply": response}''', r'''        behavior = response_data.get("behavior", "deny")
        response = "once" if behavior == "allow" else "reject"

        if self._api_v2:
            body: Dict[str, Any] = {"decision": response}
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

        body = {"reply": response}'''),

    ("fetch-models-v2-branch", r'''    async def _fetch_models(self) -> None:
        try:
            result = self._unwrap(await self._http("GET", "/provider"))''', r'''    async def _fetch_models(self) -> None:
        if self._api_v2:
            await self._fetch_models_v2()
            return
        try:
            result = self._unwrap(await self._http("GET", "/provider"))'''),

    ("fetch-models-v2-method", r'''    def set_model(self, model: str) -> None:
        self.options.model = model''', r'''    async def _fetch_models_v2(self) -> None:
        attempts, delay = 8, 1.0
        models: List[Dict[str, Any]] = []
        seen = set()
        for _ in range(attempts):
            result = None
            try:
                result = await self._http("GET", "/model")
            except Exception as error:
                LOG.debug("OpenCode model discovery failed: %s", error)
            entries = result.get("data") if isinstance(result, dict) else result
            for model in entries or []:
                if not isinstance(model, dict) or not model.get("enabled", True):
                    continue
                provider_id = model.get("providerID")
                # `id` is the addressable model; `modelID` is the family.
                model_ref = model.get("id") or model.get("modelID")
                if not provider_id or not model_ref:
                    continue
                value = f"{provider_id}/{model_ref}"
                if value in seen:
                    continue
                seen.add(value)
                models.append({
                    "displayName": model.get("name") or model_ref,
                    "description": provider_id,
                    "value": value,
                })
            if models:
                break
            await asyncio.sleep(delay)
        if models:
            self.available_models = models
            await self._message_queue.put(Message(
                "models_update", content={"models": models}
            ))

    def set_model(self, model: str) -> None:
        self.options.model = model'''),

    ("model-payload-v2", r'''        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id:
            return None
        return {"providerID": provider_id, "modelID": model_id}''', r'''        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id:
            return None
        if self._api_v2:
            # v2 Model.Ref is keyed by `id`, not `modelID`.
            return {"providerID": provider_id, "id": model_id}
        return {"providerID": provider_id, "modelID": model_id}'''),

    ("send-message-v2-branch", r'''        self._awaiting_user_message_id = True
        await self._message_queue.put(Message(
            "turn_started",
            content={},
        ))
        try:
            await self._http(
                "POST",
                f"/session/{quote(session_id, safe='')}/prompt_async",
                body=body,
            )''', r'''        self._awaiting_user_message_id = True
        self._pending_tools.clear()
        await self._message_queue.put(Message(
            "turn_started",
            content={},
        ))
        try:
            if self._api_v2:
                await self._apply_v2_session_settings(session_id, agent_name)
                asyncio.create_task(self._prompt_v2(session_id, content))
            else:
                await self._http(
                    "POST",
                    f"/session/{quote(session_id, safe='')}/prompt_async",
                    body=body,
                )'''),

    ("prompt-v2-methods", r'''    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        await self.send_message(text, proceed_plan=proceed_plan)''', r'''    async def _apply_v2_session_settings(
        self, session_id: str, agent_name: str
    ) -> None:
        try:
            await self._http(
                "POST",
                f"/session/{quote(session_id, safe='')}/agent",
                body={"agent": agent_name},
            )
        except Exception as error:
            LOG.debug("OpenCode agent selection failed: %s", error)
        model = self._model_payload()
        if model:
            try:
                # v2 Model.Ref uses "id", not "modelID" -- see 6.5
                await self._http(
                    "POST",
                    f"/session/{quote(session_id, safe='')}/model",
                    body={"model": model},
                )
            except Exception as error:
                LOG.debug("OpenCode model selection failed: %s", error)

    async def _prompt_v2(self, session_id: str, content: str) -> None:
        try:
            await self._http(
                "POST",
                f"/session/{quote(session_id, safe='')}/prompt",
                body={"text": content},
                timeout=300.0,
            )
        except Exception as error:
            if self._turn_active:
                self._turn_active = False
                self._turn_plan_mode = False
                self._awaiting_user_message_id = False
                self._turn_done.set()
                await self._message_queue.put(Message(
                    MessageType.ERROR.value, content=str(error)
                ))

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        await self.send_message(text, proceed_plan=proceed_plan)'''),

    ("interrupt-verb", r'''            await self._http(
                "POST", f"/session/{quote(self._session_id, safe='')}/abort", body={}
            )
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()''', r'''            verb = "interrupt" if self._api_v2 else "abort"
            await self._http(
                "POST",
                f"/session/{quote(self._session_id, safe='')}/{verb}",
                body={},
            )
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._pending_tools.clear()
            self._turn_done.set()'''),

    ("new-session-pending", r'''        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._permission_sessions.clear()''', r'''        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._pending_tools.clear()
        self._permission_sessions.clear()'''),

    ("rewind-v2", r'''        await self._http(
            "POST",
            f"/session/{quote(self._session_id, safe='')}/revert",
            body={"messageID": message_id},
        )
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._diff_cache.clear()
        return self._session_id''', r'''        if self._api_v2:
            # 2.0.32 keeps message granularity through the staged pair.  The
            # older DELETE form is not a message selector any more: on 2.0.32
            # it returns 204 and changes nothing, and where it does work it
            # truncates to the start of the session.  Try the staged route
            # first and fall back only when the server does not have it.
            session_path = f"/session/{quote(self._session_id, safe='')}"
            try:
                await self._http(
                    "POST",
                    f"{session_path}/revert/stage",
                    body={"messageID": message_id},
                )
            except OpenCodeHTTPError as error:
                if error.status not in (404, 405):
                    raise
                LOG.debug(
                    "OpenCode revert/stage unavailable (%s); "
                    "falling back to DELETE /revert",
                    error.status,
                )
                await self._http(
                    "DELETE",
                    f"{session_path}/revert",
                    query={"messageID": message_id},
                )
            else:
                await self._http("POST", f"{session_path}/revert/commit")
        else:
            await self._http(
                "POST",
                f"/session/{quote(self._session_id, safe='')}/revert",
                body={"messageID": message_id},
            )
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._pending_tools.clear()
        self._diff_cache.clear()
        return self._session_id'''),

    ("sync-server-fields", r'''        self.url = ""
        self.process: Optional[subprocess.Popen] = None''', r'''        self.url = ""
        self.api_v2 = False
        self.password: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None'''),

    ("sync-banner-loop", r'''        deadline = time.monotonic() + 10.0
        output = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except thread_queue.Empty:
                continue
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match:
                self.url = match.group(1).rstrip("/")
                return self''', r'''        deadline = time.monotonic() + 10.0
        password_deadline: Optional[float] = None
        output = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except thread_queue.Empty:
                continue
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match and not self.url:
                self.url = match.group(1).rstrip("/")
                self.api_v2 = _is_v2_banner(line)
                password_deadline = time.monotonic() + 2.0
                if not self.api_v2:
                    # v1 never prints a password; do not wait.
                    return self
            if self.url and not self.password:
                secret = _SERVER_PASSWORD_RE.search(line)
                if secret:
                    self.password = secret.group(1)
                    return self
                if password_deadline and time.monotonic() >= password_deadline:
                    return self'''),

    ("sync-auth-headers", r'''def _sync_auth_headers(extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    source = {**os.environ, **(extra_env or {})}
    password = source.get("OPENCODE_SERVER_PASSWORD")''', r'''def _sync_auth_headers(
    extra_env: Optional[Dict[str, str]], password: Optional[str] = None
) -> Dict[str, str]:
    source = {**os.environ, **(extra_env or {})}
    password = password or source.get("OPENCODE_SERVER_PASSWORD")'''),

    ("sync-request-v2", r'''def _sync_request(
    base_url: str,
    path: str,
    cwd: Optional[str],
    extra_env: Optional[Dict[str, str]],
) -> Any:
    query_string = "?" + urlencode({"directory": cwd}) if cwd else ""
    url = base_url + path + query_string
    request = Request(
        url,
        headers={"Accept": "application/json", **_sync_auth_headers(extra_env)},
        method="GET",
    )''', r'''def _sync_request(
    base_url: str,
    path: str,
    cwd: Optional[str],
    extra_env: Optional[Dict[str, str]],
    api_v2: bool = False,
    password: Optional[str] = None,
) -> Any:
    if api_v2 and not path.startswith("/api"):
        path = "/api" + path
    query_string = "?" + urlencode({"directory": cwd}) if cwd else ""
    url = base_url + path + query_string
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            **_sync_auth_headers(extra_env, password),
        },
        method="GET",
    )'''),

    ("list-sessions-callsite", r'''            sessions = _sync_request(server.url, "/session", cwd, extra_env)''', r'''            sessions = _sync_request(
                server.url,
                "/session",
                cwd,
                extra_env,
                api_v2=server.api_v2,
                password=server.password,
            )'''),

    ("session-info-callsites", r'''            session = _sync_request(server.url, f"/session/{sid}", cwd, extra_env)
            messages = _sync_request(
                server.url, f"/session/{sid}/message", cwd, extra_env
            )''', r'''            session = _sync_request(
                server.url,
                f"/session/{sid}",
                cwd,
                extra_env,
                api_v2=server.api_v2,
                password=server.password,
            )
            messages = _sync_request(
                server.url,
                f"/session/{sid}/message",
                cwd,
                extra_env,
                api_v2=server.api_v2,
                password=server.password,
            )'''),

    ("session-info-shape", r'''            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") or {}
                text = "".join(
                    part.get("text", "")
                    for part in entry.get("parts", [])
                    if isinstance(part, dict) and part.get("type") == "text"
                    and not part.get("ignored")
                ).strip()
                if info.get("role") == "user" and text:
                    if current is not None:
                        turns.append(current)
                    current = {"prompt": text, "response": None}
                elif info.get("role") == "assistant" and text and current is not None:''', r'''            for entry in entries:
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
                    if current is not None:
                        turns.append(current)
                    current = {"prompt": text, "response": None}
                elif role == "assistant" and text and current is not None:'''),
]


def main() -> int:
    raw = open(TARGET, "r", encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")

    results = []
    failures = []
    for label, old, new in EDITS:
        # Already applied? For insert-style edits `old` (the anchor) may also
        # still be present, so test `new` only.
        if new in text:
            results.append((label, "SKIP (already applied)"))
            continue
        count = text.count(old)
        if count != 1:
            results.append((label, f"FAIL anchor count={count}"))
            failures.append(label)
            continue
        text = text.replace(old, new)
        results.append((label, "OK"))

    for label, status in results:
        print(f"{status:26s} {label}")

    if failures:
        print(f"\n{len(failures)} failure(s); nothing written: {failures}")
        return 1

    if not any(status == "OK" for _, status in results):
        print("\nNo changes needed; file left untouched (already patched).")
        return 0

    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    shutil.copy2(TARGET, BACKUP)
    out = text.replace("\n", nl)
    open(TARGET, "w", encoding="utf-8", newline="").write(out)
    print(f"\nAll {len(results)} edits applied. Backup: {BACKUP}")
    print("bytes before:", len(raw), "after:", len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
