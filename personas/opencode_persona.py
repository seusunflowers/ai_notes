r"""Local Jupyter AI persona: OpenCode (ACP), with Windows workarounds.

Two upstream problems are worked around here:

1. Version gate. The `@jupyter-ai/acp-client` package ships an `opencode-acp`
   persona that refuses to load unless the `opencode` CLI is >=1.0.0,<2. That
   check also runs `subprocess.run(["opencode", "--version"])`, which fails on
   Windows for *any* version, because the npm shim is `opencode.cmd`, a batch
   file that `CreateProcess` cannot launch.

2. Subprocess spawning. `jupyter_server` forces
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
  * opencode CLI installed and on PATH
  * the folder holding the real `opencode.exe`
    (`%APPDATA%\npm\node_modules\@opencode\cli\bin`) must come BEFORE
    `%APPDATA%\npm` in PATH, otherwise only the .cmd shim is found and spawning fails.
  * Run `opencode auth` once, or export your provider's API key, before
    starting JupyterLab.
"""

import asyncio
import os
import platform
import queue
import subprocess
import threading
from pathlib import Path

from acp.exceptions import RequestError
from jupyter_ai_persona_manager import PersonaDefaults
from jupyterlab_chat.models import Message

from jupyter_ai_acp_client.base_acp_persona import BaseAcpPersona

_STREAM_LIMIT = 50 * 1024 * 1024
_WRITE_HIGH_WATER = 4 * 1024 * 1024


def _needs_pipe_shim(loop: asyncio.AbstractEventLoop) -> bool:
    """True when this loop cannot spawn subprocesses / connect pipes."""
    return platform.system() == "Windows" and isinstance(loop, asyncio.SelectorEventLoop)


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

        # Ask OpenCode to confirm edits / shell commands before running them.
        if "OPENCODE_CONFIG" not in os.environ and not _has_user_config():
            env = os.environ.copy()
            env["OPENCODE_CONFIG"] = _acp_pkg("acp_personas", "opencode.json")

        loop = asyncio.get_running_loop()
        if _needs_pipe_shim(loop):
            self.log.info(
                "SelectorEventLoop cannot spawn subprocesses; using the "
                "thread-based pipe shim to launch '%s'.",
                " ".join(self._executable),
            )
            return await _spawn_process(list(self._executable), env)

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