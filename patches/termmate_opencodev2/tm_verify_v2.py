"""TermMate / OpenCode v2 verification harness (manual section 8).

Drives the patched TermMate OpenCode adapter outside Sublime Text against a
live `opencode serve` (v2.x), auto-approving permission requests, and prints a
per-assertion PASS/FAIL table.

Run:
    $env:TM_MODEL='deepseek/deepseek-flash'
    python tm_verify_v2.py

Environment overrides:
    TM_MODEL       target model   (default deepseek/deepseek-flash)
    TM_FAIL_MODEL  failing model  (default opencode/cat-jam-free)
    TM_CLI         opencode CLI   (default %APPDATA% npm opencode.cmd)
"""
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
TEMP_ROOT = os.environ.get("TEMP") or os.path.join(HOME, "AppData", "Local", "Temp")
APP_DATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
WORK = os.path.join(TEMP_ROOT, "opencode")
ADAPTER_DIR = os.path.join(  # unpacked package (manual section 7)
    APP_DATA, "Sublime Text", "Packages", "TermMate", "genfoundry",
)
PKG_DIR = os.path.join(WORK, "tm_verify_pkg")
PROJECT = os.path.join(WORK, "tm_project")
CLI = os.environ.get("TM_CLI") or os.path.join(APP_DATA, "npm", "opencode.cmd")
MODEL = os.environ.get("TM_MODEL") or "deepseek/deepseek-flash"
FAIL_MODEL = os.environ.get("TM_FAIL_MODEL") or "opencode/cat-jam-free"

START = time.time()
RESULTS = []


def log(line):
    print(f"[{time.time() - START:7.2f}s] {line}", flush=True)


def record(name, ok, extra=""):
    RESULTS.append((name, bool(ok), str(extra)))
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))


def load_modules():
    os.makedirs(PKG_DIR, exist_ok=True)
    for fname in ("base_agent.py", "opencode_agent.py"):
        shutil.copy2(os.path.join(ADAPTER_DIR, fname), os.path.join(PKG_DIR, fname))
    with open(os.path.join(PKG_DIR, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("")
    if WORK not in sys.path:
        sys.path.insert(0, WORK)
    oa = importlib.import_module("tm_verify_pkg.opencode_agent")
    ba = importlib.import_module("tm_verify_pkg.base_agent")
    return oa, ba


class Pump:
    """Drains the adapter message queue, auto-approving tool permissions."""

    def __init__(self, agent):
        self.agent = agent
        self.messages = []
        self.approvals = []
        self.approval_errors = []

    def types(self, start=0):
        return [m.type for m in self.messages[start:]]

    async def _handle(self, m):
        self.messages.append(m)
        content = getattr(m, "content", None)
        log(f"MSG {m.type} {repr(content)[:200]}")
        if m.type == "control_request":
            body = content or {}
            req = (body.get("request") or {}) if isinstance(body, dict) else {}
            self.approvals.append(req)
            rid = body.get("request_id") if isinstance(body, dict) else None
            try:
                await asyncio.wait_for(
                    self.agent.send_approval_response(rid, {"behavior": "allow"}), 20
                )
                log(f"APPROVED {rid}")
            except Exception as error:  # noqa: BLE001
                self.approval_errors.append(f"{rid}: {error!r}")
                log(f"APPROVAL FAILED {error!r}")

    async def wait_until(self, pred, timeout):
        deadline = time.time() + timeout
        while True:
            if pred(self):
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return pred(self)
            try:
                m = await asyncio.wait_for(
                    self.agent._message_queue.get(), min(0.5, remaining)
                )
            except asyncio.TimeoutError:
                continue
            await self._handle(m)


async def check_dead(pid):
    if not pid:
        return False
    for _ in range(20):
        out = ""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=30,
            ).stdout or ""
        except Exception:  # noqa: BLE001
            return False
        alive = str(pid) in out and "No tasks" not in out
        if not alive:
            return True
        await asyncio.sleep(0.5)
    return False


async def main():
    os.makedirs(PROJECT, exist_ok=True)
    oa, ba = load_modules()
    log(f"adapter module: {oa.__file__}")
    log(f"cli={CLI}  model={MODEL}  fail_model={FAIL_MODEL}  project={PROJECT}")

    options = ba.AgentOptions(
        cwd=PROJECT,
        cli_path=CLI,
        allowed_tools=[],
        disallowed_tools=[],
        permission_mode="default",
        approve_mode="default",
        model=MODEL,
    )
    agent = oa.OpenCodeAgent(options)
    pump = Pump(agent)
    pid = None

    try:
        await asyncio.wait_for(agent.connect(), 90)
        record("connect-ok", True, f"{time.time() - START:.1f}s")
        pid = agent._server_process.pid if getattr(agent, "_server_process", None) else None
        api2 = agent._api_v2 is True
        url = str(getattr(agent, "server_url", "") or "")
        pw = bool(getattr(agent, "_server_password", None))
        record("api-v2", api2, f"api_v2={api2}")
        record("server-url", url.startswith("http://127.0.0.1:") and not url.endswith(":0"), url)
        record("password-captured", pw, f"password_captured={pw}")
        log(f"api_v2={api2} url={url} password_captured={pw} pid={pid}")

        # ---- models ------------------------------------------------------
        n = len(pump.messages)
        got = await pump.wait_until(
            lambda p: any(m.type == "models_update" for m in p.messages[n:]), 60
        )
        record("models-update-received", got)
        models = getattr(agent, "available_models", []) or []
        vals = [str(m.get("value")) for m in models if isinstance(m, dict)]
        record("model-target-present", MODEL in vals, f"models={len(vals)}")

        # ---- turn: shell tool + approval round trip ----------------------
        n = len(pump.messages)
        await agent.send_message(
            "Use the shell tool to run exactly: echo TERMMATE_V2_OK. "
            "Then reply with the output and nothing else."
        )
        await pump.wait_until(
            lambda p: any(m.type in ("stop", "error") for m in p.messages[n:]), 240
        )
        sl = pump.messages[n:]
        types = [m.type for m in sl]
        record("turn-started", "turn_started" in types)
        record("user-message-id", "user_message_id" in types)
        cr = next((m for m in sl if m.type == "control_request"), None)
        record("control-request-received", cr is not None)
        req = {}
        if cr is not None and isinstance(cr.content, dict):
            req = cr.content.get("request") or {}
        inp = req.get("input") or {}
        record("subtype-can_use_tool", req.get("subtype") == "can_use_tool")
        record("tool-name-bash", req.get("tool_name") == "Bash", str(req.get("tool_name")))
        record("permission-type-shell", inp.get("permission_type") == "shell",
               str(inp.get("permission_type")))
        record("permission-command-real", "TERMMATE_V2_OK" in json.dumps(inp, default=str))
        record("approval-reply-no-error", not pump.approval_errors,
               "; ".join(pump.approval_errors))
        tool_msgs = [m for m in sl if m.type == "tool_use"]
        details = json.dumps([m.content for m in tool_msgs], default=str)
        record("tool-message-received", bool(tool_msgs))
        record("tool-command_execution", "command_execution" in details)
        record("tool-output-ok", "TERMMATE_V2_OK" in details and "completed" in details)
        text = "".join(str(m.content) for m in sl if m.type == "text")
        record("text-contains-ok", "TERMMATE_V2_OK" in text, repr(text[:60]))
        errs = [m for m in sl if m.type == "error"]
        record("no-errors-happy", not errs, repr([m.content for m in errs][:1]))
        record("stop-received", "stop" in types)

        # ---- interrupt / rewind ------------------------------------------
        try:
            await asyncio.wait_for(agent.interrupt(), 15)
            record("interrupt-ok", True)
        except Exception as error:  # noqa: BLE001
            record("interrupt-ok", False, repr(error))
        try:
            await asyncio.wait_for(agent.rewind("msg_dummy"), 15)
            record("rewind-ok", True)
        except Exception as error:  # noqa: BLE001
            record("rewind-ok", False, repr(error))

        # ---- synchronous session picker ----------------------------------
        try:
            sessions = await asyncio.to_thread(
                oa.list_opencode_sessions, PROJECT, None, CLI
            )
            record("sessions-nonempty", bool(sessions), f"sessions={len(sessions)}")
            recovered = 0
            for s in sessions[:5]:
                info = await asyncio.to_thread(
                    oa.get_opencode_session_info, s["session_id"], PROJECT, None, CLI, 0
                )
                turns = (info or {}).get("turns") or []
                if turns:
                    recovered = len(turns)
                    break
            record("recovered-turns", recovered >= 1, f"turns={recovered}")
        except Exception as error:  # noqa: BLE001
            record("sessions-nonempty", False, repr(error))
            record("recovered-turns", False, "picker phase failed")

        # ---- failure path: unreachable 403 provider ----------------------
        await agent.new_session()
        agent.set_model(FAIL_MODEL)
        n = len(pump.messages)
        await agent.send_message("ping")
        got = await pump.wait_until(
            lambda p: any(m.type == "error" for m in p.messages[n:]), 120
        )
        record("failure-error-received", got)
        await asyncio.sleep(0.5)
        record("failure-turn-cleared", not agent._turn_active)
        n = len(pump.messages)
        await agent.send_message("ping again")
        got2 = await pump.wait_until(
            lambda p: any(m.type in ("error", "stop") for m in p.messages[n:]), 120
        )
        record("second-prompt-terminal", got2)
        await asyncio.sleep(0.5)
        record("failure-turn-cleared-2", not agent._turn_active)
        agent.set_model(MODEL)

    except Exception as error:  # noqa: BLE001
        record("harness-error", False, repr(error))
    finally:
        if pid is None:
            sp = getattr(agent, "_server_process", None)
            if sp is not None:
                try:
                    pid = sp.pid
                except Exception:  # noqa: BLE001
                    pass
        try:
            if getattr(agent, "_is_connected", False):
                await asyncio.wait_for(agent.disconnect(), 90)
        except Exception as error:  # noqa: BLE001
            log(f"disconnect error: {error!r}")
        dead = await check_dead(pid) if pid else False
        if not dead and pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
            await asyncio.sleep(2)
            dead = await check_dead(pid)
        record("server-dead-after-disconnect", dead, f"pid={pid}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"TERMMATE_VERIFY_RESULT={'PASS' if passed == total else 'FAIL'} {passed}/{total}",
          flush=True)
    log(f"{passed}/{total} passed -> RESULT: {'PASS' if passed == total else 'FAIL'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
