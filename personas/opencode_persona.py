r"""Local Jupyter AI persona: OpenCode (ACP), with Windows workarounds.

Three upstream problems are worked around here:

1. Version gate. The `@jupyter-ai/acp-client` package ships an `opencode-acp`
   persona that refuses to load unless the `opencode` CLI is >=1.0.0,<2. That
   check also runs `subprocess.run(["opencode", "--version"])`, which fails on
   Windows for *any* version, because the npm shim is `opencode.cmd`, a batch
   file that `CreateProcess` cannot launch.

2. Executable resolution. `CreateProcess` only ever appends `.exe`, so a bare
   `"opencode"` argument resolves to the npm `.cmd` shim and dies with
   `FileNotFoundError: [WinError 2]`. Fixing PATH order is unreliable on
   Windows -- the effective PATH is (Machine PATH ; User PATH), so an nvm4w /
   Program Files node install beats anything prepended to the User PATH, and
   every opencode upgrade can move the binary again. `resolve_opencode_executable`
   therefore finds the real `opencode.exe` itself: explicit env override, then
   every npm global prefix (asked from `npm config get prefix`, plus `%APPDATA%\npm`
   and PATH entries), then the path embedded in the `.cmd` shim text, then any
   PATH directory that literally holds `opencode.exe`. Newest install wins.

3. Subprocess spawning. `jupyter_server` forces
   `asyncio.WindowsSelectorEventLoopPolicy` on Windows (serverapp.py,
   `_init_asyncio_patch`), and a `SelectorEventLoop` cannot create subprocesses
   or connect pipes at all -- it raises a bare `NotImplementedError`. This makes
   *every* ACP persona unusable on Windows. `_spawn_process` below builds the
   process from `subprocess.Popen` and wires its pipes to real
   `asyncio.StreamReader`/`StreamWriter` objects via plain reader/writer
   threads, which is the same trick `acp.stdio` uses for its own Windows stdio.

This persona is loaded automatically from the workspace `.jupyter/personas/`
directory (NOT from `~/.jupyter/personas/` -- Jupyter AI only looks inside the
JupyterLab root dir and its subdirectories).

Prerequisites:
  * opencode CLI installed somewhere npm can see it. The persona locates the
    real `opencode.exe` itself (see `resolve_opencode_executable`), so PATH
    order does not matter -- which matters because Windows builds the effective
    PATH as (Machine PATH ; User PATH) and an nvm4w / Program Files node
    install always wins over anything prepended to the User PATH.
    Optional overrides: `OPENCODE_ACP_EXE`, `OPENCODE_NPM_PREFIX`.
  * Run `opencode auth` once, or export your provider's API key, before
    starting JupyterLab.
"""

import asyncio
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path

from acp.exceptions import RequestError
from jupyter_ai_persona_manager import PersonaDefaults
from jupyterlab_chat.models import Message

from jupyter_ai_acp_client.base_acp_persona import BaseAcpPersona

_STREAM_LIMIT = 50 * 1024 * 1024
_WRITE_HIGH_WATER = 4 * 1024 * 1024

# Only `.exe`/`.com` can be launched by CreateProcess. npm's shims are
# `.cmd`/`.ps1`/extensionless shell scripts, which is what makes a naive
# `create_subprocess_exec("opencode", ...)` raise
# `FileNotFoundError: [WinError 2]`.
_SPAWNABLE_EXTS = (".exe", ".com")


def _needs_pipe_shim(loop: asyncio.AbstractEventLoop) -> bool:
    """True when this loop cannot spawn subprocesses / connect pipes."""
    return platform.system() == "Windows" and isinstance(loop, asyncio.SelectorEventLoop)


# --------------------------------------------------------------------------
# Locating the real opencode.exe
# --------------------------------------------------------------------------
#
# PATH order is NOT a reliable fix here, because Windows composes the effective
# PATH as (Machine PATH ; User PATH). Once `npm config get prefix` points at a
# directory that lives in the *Machine* PATH (e.g. nvm4w's `C:\nvm4w\nodejs`),
# no amount of prepending inside the User PATH can win: the `.cmd` shim in the
# Machine PATH dir is always found first. So resolve the binary ourselves.

_EXE_NAME = "opencode.exe"
_REL_BIN = ("node_modules", "@opencode", "cli", "bin", _EXE_NAME)
_SHIM_EXE_RE = re.compile(r'"([^"\n]*opencode\.exe)"', re.IGNORECASE)


def _npm_global_prefixes() -> list[Path]:
    """Every plausible npm global prefix, newest-install-friendly order."""
    prefixes: list[Path] = []

    env_prefix = os.environ.get("OPENCODE_NPM_PREFIX")
    if env_prefix:
        prefixes.append(Path(env_prefix))

    # Ask npm itself. `npm` is itself a .cmd shim, so it needs cmd.exe.
    for launcher in (["cmd", "/c", "npm", "config", "get", "prefix"],):
        try:
            done = subprocess.run(  # noqa: S603
                launcher,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        reported = (done.stdout or "").strip().splitlines()
        if done.returncode == 0 and reported:
            prefixes.append(Path(reported[-1].strip()))

    appdata = os.environ.get("APPDATA")
    if appdata:
        prefixes.append(Path(appdata) / "npm")

    prefixes += [Path(p) for p in (os.environ.get("PATH") or "").split(os.pathsep) if p]
    prefixes += [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "npm",
    ]

    seen: set[str] = set()
    ordered: list[Path] = []
    for prefix in prefixes:
        key = str(prefix).lower().rstrip("\\/")
        if key and key not in seen:
            seen.add(key)
            ordered.append(prefix)
    return ordered


def _exes_under(prefix: Path) -> list[Path]:
    exe = prefix.joinpath(*_REL_BIN)
    return [exe] if exe.is_file() else []


def _exes_on_path() -> list[Path]:
    """opencode.exe sitting directly in a PATH directory."""
    found: list[Path] = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry:
            continue
        for ext in _SPAWNABLE_EXTS:
            candidate = Path(entry) / ("opencode" + ext)
            if candidate.is_file():
                found.append(candidate)
    return found


def _exe_from_shim() -> list[Path]:
    """Dig the real .exe path out of the npm .cmd shim text."""
    shim = shutil.which("opencode")
    if not shim or shim.lower().endswith(_SPAWNABLE_EXTS):
        return []
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Path] = []
    for match in _SHIM_EXE_RE.finditer(text):
        raw = match.group(1).replace("\\", " ").split(" ")[0].replace("/", "\\")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(shim).parent / candidate
        if candidate.is_file():
            out.append(candidate)
    return out


def resolve_opencode_executable() -> str:
    """Absolute path to a CreateProcess-launchable `opencode.exe`.

    Falls back to the bare name so the original FileNotFoundError surfaces
    if nothing at all can be found.
    """
    override = os.environ.get("OPENCODE_ACP_EXE")
    if override and Path(override).is_file():
        return override

    candidates: list[Path] = []
    for prefix in _npm_global_prefixes():
        candidates.extend(_exes_under(prefix))
    candidates.extend(_exe_from_shim())
    candidates.extend(_exes_on_path())

    # Newest install first, so a stale leftover copy never wins.
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    unique = {str(p).lower(): p for p in candidates if p.is_file()}
    ranked = sorted(unique.values(), key=_mtime, reverse=True)
    return str(ranked[0]) if ranked else "opencode"


# --------------------------------------------------------------------------
# Windows subprocess shim
# --------------------------------------------------------------------------


class _PipeWriteProtocol(asyncio.BaseProtocol):
    """Protocol that turns transport pause/resume into StreamWriter.drain()."""

    def __init__(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._paused = False
        self._waiter: asyncio.Future | None = None

    def pause_writing(self) -> None:
        self._paused = True
        if self._waiter is None:
            self._waiter = self._loop.create_future()

    def resume_writing(self) -> None:
        self._paused = False
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)
        self._waiter = None

    async def _drain_helper(self) -> None:
        """Awaited by ``StreamWriter.drain()`` while the pipe is congested."""
        if self._paused and self._waiter is not None:
            await self._waiter

    def _eof(self) -> None:
        self.resume_writing()


class _PipeWriteTransport(asyncio.BaseTransport):
    """asyncio transport that hands writes to a dedicated writer thread."""

    def __init__(
        self,
        items: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        protocol: "_PipeWriteProtocol",
    ) -> None:
        self._items = items
        self._loop = loop
        self._closing = False
        self._pending_bytes = 0
        self._protocol = protocol

    def write(self, data: bytes) -> None:
        if self._closing:
            return
        payload = bytes(data)
        if not payload:
            return
        self._pending_bytes += len(payload)
        self._items.put(payload)
        if self._pending_bytes > _WRITE_HIGH_WATER:
            self._protocol.pause_writing()
        else:
            self._protocol.resume_writing()

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._items.put(None)

    def abort(self) -> None:
        self.close()

    def is_closing(self) -> bool:
        return self._closing

    def can_write_eof(self) -> bool:
        return False

    def get_extra_info(self, name: str, default=None):
        return default


class _PipeProcess:
    """Duck-typed stand-in for `asyncio.subprocess.Process`.

    Exposes the small surface Jupyter AI / the `acp` library actually use:
    `.stdin` (StreamWriter), `.stdout` (StreamReader), `.pid`, `.returncode`,
    `.wait()`, `.terminate()` and `.kill()`.
    """

    def __init__(
        self,
        popen: subprocess.Popen,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        transport: _PipeWriteTransport,
    ) -> None:
        self._popen = popen
        self.stdin = writer
        self.stdout = reader
        self.stderr = None
        self._transport = transport

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    async def wait(self) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._popen.wait)

    def terminate(self) -> None:
        with _suppress():
            self._popen.terminate()

    def kill(self) -> None:
        with _suppress():
            self._popen.kill()

    def send_signal(self, sig) -> None:
        with _suppress():
            self._popen.send_signal(sig)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None


async def _spawn_process(command: list[str], env: dict[str, str] | None) -> _PipeProcess:
    """Spawn `command` and expose its stdio as asyncio streams, on any loop."""
    loop = asyncio.get_running_loop()

    popen = await loop.run_in_executor(
        None,
        lambda: subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit the server's stderr, like the base persona
            bufsize=0,
            env=env,
        ),
    )

    # --- stdout: blocking reads on a thread, fed into a StreamReader --------
    reader = asyncio.StreamReader(limit=_STREAM_LIMIT, loop=loop)
    out_fd = popen.stdout.fileno()

    def _pump() -> None:
        try:
            while True:
                chunk = os.read(out_fd, 65536)
                if not chunk:
                    break
                loop.call_soon_threadsafe(reader.feed_data, chunk)
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=_pump, name="acp-stdout-pump", daemon=True).start()

    # --- stdin: writes on a thread, surfaced through a real StreamWriter ----
    items: queue.Queue = queue.Queue()
    protocol = _PipeWriteProtocol()
    transport = _PipeWriteTransport(items, loop, protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)

    in_fd = popen.stdin.fileno()

    def _writer_thread() -> None:
        while True:
            payload = items.get()
            if payload is None:
                break
            try:
                os.write(in_fd, payload)
            except Exception:
                break
            if items.empty():
                loop.call_soon_threadsafe(protocol.resume_writing)

    threading.Thread(
        target=_writer_thread, name="acp-stdin-writer", daemon=True
    ).start()

    return _PipeProcess(popen, reader, writer, transport)


# --------------------------------------------------------------------------
# Persona
# --------------------------------------------------------------------------


def _acp_pkg(*parts: str) -> str:
    """Absolute path to a file shipped inside the jupyter_ai_acp_client package."""
    import jupyter_ai_acp_client

    return os.path.abspath(
        os.path.join(os.path.dirname(jupyter_ai_acp_client.__file__), *parts)
    )


def _is_auth_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        keyword in message
        for keyword in (
            "api key",
            "api_key",
            "authentication",
            "authorized",
            "credential",
            "forbidden",
            "not configured",
        )
    )


def _has_user_config() -> bool:
    config_dir = Path.home() / ".config" / "opencode"
    return (config_dir / "opencode.json").exists() or (config_dir / "opencode.jsonc").exists()


class OpenCodePersona(BaseAcpPersona):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, executable=["opencode", "acp"], **kwargs)

    async def _resolve_executable(self) -> list[str]:
        """Resolve `opencode acp` to an absolute, spawnable command line."""
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(None, resolve_opencode_executable)
        command = [resolved, "acp"]
        if command != self._executable:
            self.log.info(
                "[OpenCode] Resolved ACP agent command: %s (PATH lookup gave %r).",
                command[0],
                shutil.which("opencode"),
            )
        self._executable = command
        return command

    @property
    def defaults(self) -> PersonaDefaults:
        return PersonaDefaults(
            name="OpenCode",
            description="OpenCode as an ACP agent persona (local, no version gate).",
            avatar_path=_acp_pkg("static", "opencode.svg"),
            system_prompt="unused",
        )

    async def before_agent_subprocess(self):
        return None

    async def _init_agent_subprocess(self):
        env: dict[str, str] | None = None

        # Never rely on PATH: see resolve_opencode_executable().
        command = await self._resolve_executable()

        # Ask OpenCode to confirm edits / shell commands before running them.
        if "OPENCODE_CONFIG" not in os.environ and not _has_user_config():
            env = os.environ.copy()
            env["OPENCODE_CONFIG"] = _acp_pkg("acp_personas", "opencode.json")

        loop = asyncio.get_running_loop()
        if _needs_pipe_shim(loop):
            self.log.info(
                "SelectorEventLoop cannot spawn subprocesses; using the "
                "thread-based pipe shim to launch '%s'.",
                " ".join(command),
            )
            return await _spawn_process(command, env)

        # Bypass super() only to keep `limit`/`start_new_session` parity:
        # it reads self._executable, which we have just refreshed anyway.
        self._executable = command
        return await super()._init_agent_subprocess(env=env)

    async def is_authed(self) -> bool:
        return True

    async def process_message(self, message: Message) -> None:
        try:
            await super().process_message(message)
        except RequestError as error:
            if not _is_auth_error(error):
                raise
            self.log.info("[OpenCode] Authentication required: %s", error)
            await self.handle_message_no_auth(message)

    async def handle_message_no_auth(self, message: Message | None = None) -> None:
        await super().handle_message_no_auth(message)
        self.send_message(
            "OpenCode isn't configured yet.\n\n"
            "- Run `opencode auth` in a terminal to configure your LLM provider.\n"
            "- Or set your provider's API key as an environment variable "
            "(e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) before starting JupyterLab.\n"
            "- If you set it in a new shell, restart the JupyterLab server."
        )