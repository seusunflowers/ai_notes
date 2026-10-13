# 在 Windows 上让 JupyterLab 使用 OpenCode（Jupyter AI 3.x / ACP）

> 面向 agent 的操作手册。目标是让另一台 Windows 电脑上的 agent 读完这份文件后，
> 能独立诊断并修好「JupyterLab 里用不了 OpenCode」的问题。
>
> 实测环境：Windows / Python 3.14 / JupyterLab 4.6.4 / jupyter_ai 3.2.0 /
> jupyterlab-chat 0.25.1 / jupyter_ai_acp_client 0.3.3 / opencode 2.0.27

---

## 0. 先读这段：这里有三个互相独立的问题

不要试图一次修完。它们症状相似，但根因完全不同，**必须按顺序排查**。

| # | 症状 | 根因 | 章节 |
|---|---|---|---|
| 1 | 左侧有**两个**图标；点 "Jupyter AI Chat" 报 "problem with the Chat backend"；控制台反复刷 `404 GET /api/ai/chats` | jupyter_ai 升到 3.x 后，早先 2.x 的前端扩展 `@jupyter-ai/core` 变成孤儿文件，仍在请求 3.x 已删除的 `/api/ai/*` 后端 | §2 |
| 2 | "Jupyter Chat" 里**没有 Agent 选项**，或选了 Agent 也加载不出来 | persona 列表为空。8 个内置 persona 在 Windows 上全部不可用；本地 persona 又被放在了错误的目录 | §3 §4 |
| 3 | 选了 Agent，发送消息报 `NotImplementedError:`（**错误信息为空**） | jupyter_server 在 Windows 上强制 `SelectorEventLoop`，该事件循环**无法创建子进程**。这是上游缺陷，影响所有 ACP Agent | §5 |

**关键判据**：`NotImplementedError:` 后面什么都没有 = 事件循环问题，不要往 opencode 或 API key 方向查。

---

## 1. 环境探测（先跑这些，不要凭猜测改配置）

在 PowerShell 里执行：

```powershell
# 版本全景
jupyter --version
pip list | Select-String "jupyter"

# 前端扩展清单 —— 看有没有 @jupyter-ai/core（问题 1 的信号）
$env:NO_COLOR="1"; jupyter labextension list

# 所有 labextensions 的真实位置
python -c "from jupyter_core.paths import jupyter_path; print('\n'.join(jupyter_path('labextensions')))"

# JupyterLab 的 root_dir（决定本地 persona 放哪，见 §4）
Get-Content $env:USERPROFILE\.jupyter\jupyter_lab_config.py | Select-String "notebook_dir|root_dir"
```

```python
# 用 Python 确认哪些 persona 可用（问题 2 的直接证据）。
# 期望输出里 AVAILABLE 应该至少有一个 opencode。
python -c "
from importlib_metadata import entry_points
from jupyter_ai_persona_manager import PersonaRequirementsUnmet
for ep in entry_points().select(group='jupyter_ai.personas'):
    try:
        ep.load(); print('AVAILABLE   ', ep.name)
    except Exception as e:
        print('unavailable ', ep.name, '|', str(e)[:80])
"
```

---

## 2. 修复问题 1：删除孤儿扩展 `@jupyter-ai/core`

### 识别

```powershell
jupyter labextension list | Select-String "@jupyter-ai"
```

- `@jupyter-ai/acp-client`、`@jupyter-ai/router`、`@jupyter-ai/persona-manager`、`@jupyter-ai/chat-commands` → 正常，3.x 需要
- `@jupyter-ai/core` → **孤儿**，来自 jupyter_ai 2.x

确认它是孤儿（没有任何已安装的 Python 包声明拥有它）：

```python
import glob, os
import site
sp = site.getusersitepackages()
owners = {}
for rec in glob.glob(os.path.join(sp, "*.dist-info", "RECORD")):
    pkg = os.path.basename(os.path.dirname(rec))
    for line in open(rec, encoding="utf-8", errors="replace").read().splitlines():
        p = line.split(",")[0]
        if "labextensions/@jupyter-ai/core" in p:
            owners.setdefault(pkg, 0)
            owners[pkg] += 1
print(owners or "ORPHAN -> no owning distribution")
```

### 修复

`jupyter labextension uninstall @jupyter-ai/core` **会失败**，它把包名映射到 `jupyter_ai`，而 `jupyter_ai` 3.x 并不知道这个扩展。直接删目录：

```powershell
$ext = "<labextensions 路径>\@jupyter-ai\core"     # 用 §1 输出的路径
Copy-Item $ext "$env:TEMP\orphan-jupyter-ai-core-backup" -Recurse -Force   # 备份
Remove-Item $ext -Recurse -Force
```

验证：`jupyter labextension list | Select-String "@jupyter-ai/core"` 应该没有输出。

> 顺带说明：`jupyter_ai` 3.2.0 的 Python 包是**纯元包**（`__init__.py` 只有 `DEFAULT_JUPYTER_SERVER_MCP_TOOLS`），
> 它**不提供** `/api/ai/*` 后端。启动日志里的
> `jupyter_ai | extension failed loading with message: ExtensionLoadingError(...)`
> 是**正常现象**，不用管。

---

## 3. 修复问题 2 之一：让 `opencode` 可被真正启动

### 3.1 npm 的 `.cmd` shim 无法被 CreateProcess 启动

`jupyter_ai_acp_client` 用 `subprocess.run(["opencode","--version"])` 和
`asyncio.create_subprocess_exec("opencode","acp")` 启动 opencode。
Windows 的 CreateProcess 在 PATH 里**只尝试补 `.exe`**，不认 `.cmd` / `.bat`，于是：

```
FileNotFoundError: [WinError 2] 系统找不到指定的文件
```

但 npm 其实装了真的 exe，只是被 `.cmd` 挡住了：

```powershell
Get-ChildItem "$env:APPDATA\npm" -Filter "opencode*"
Get-Content "$env:APPDATA\npm\opencode.cmd"     # 能看到真正的 exe 路径
```

修复：把**存放 `opencode.exe` 的目录**插到用户 PATH 的**最前面**。
顺序是关键 —— 必须排在 `$env:APPDATA\npm` 之前，否则 `shutil.which` 只会返回 `.cmd`。

```powershell
$bin = "$env:APPDATA\npm\node_modules\@opencode\cli\bin"
if (-not (Test-Path "$bin\opencode.exe")) { throw "找不到 opencode.exe，请先 npm install -g opencode-ai" }

$user = [Environment]::GetEnvironmentVariable("Path","User")
$parts = $user -split ';' | Where-Object { $_ -and $_.Trim() -ne $bin }
[Environment]::SetEnvironmentVariable("Path", (($bin + ";" + ($parts -join ';')).TrimEnd(';')), "User")
```

验证：

```powershell
# 注意：新 PATH 只对新进程生效，这里要显式刷新
$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
python -c "import shutil; print(shutil.which('opencode'))"
# 期望输出以 .EXE 结尾，不是 .CMD
```

### 3.2 内置 persona 的版本门

`@jupyter-ai/acp-client` 自带的 `opencode-acp` persona 要求 `>=1.0.0,<2`，
而 npm 上的 opencode 已经是 2.x，会被直接拒绝：

```
opencode version 2.0.27 is installed, but version >=1.0.0,<2 is required.
```

**不要降级 opencode**（会破坏用户已有环境）。改用 §4 的本地 persona 绕过。

### 3.3 确认 opencode 已认证

```powershell
cmd /c "opencode auth list"
```

输出里至少要有一个 `stored` 或 `environment` 的 provider。
否则在 JupyterLab 里会得到 "OpenCode isn't configured yet"。

---

## 4. 修复问题 2 之二：本地 persona 的位置（最容易踩的坑）

Jupyter AI 加载本地 persona 的逻辑是
`PersonaManager.get_dotjupyter_dir()` → `find_dot_dir(chat_dir, ".jupyter", root_dir=...)`：

> 从**聊天文件所在目录逐级向上**找 `.jupyter`，**最远只到 JupyterLab 的 root_dir**。

所以：

- ✅ 正确：`<JupyterLab root_dir>\.jupyter\personas\*.py`
- ❌ 错误：`%USERPROFILE%\.jupyter\personas\`（用户主目录不在查找路径上，永远不会被读）

先用 §1 的方法确定 root_dir，然后：

```powershell
$root = "C:\path\to\your\notebook\root"      # ← 换成实际值
New-Item -ItemType Directory -Force -Path "$root\.jupyter\personas"
```

> 换 root_dir 启动 JupyterLab 时，必须在新根目录下再放一份这个文件。
>
> **规范源**：本手册所在的仓库内 `personas/opencode_persona.py`。
> 修改请改规范源，再复制到目标 `<root_dir>\.jupyter\personas\`，避免多机漂移。

---

## 5. 修复问题 3：Windows 上无法创建子进程（核心难点）

### 根因

```python
# jupyter_server/serverapp.py, ServerApp._init_asyncio_patch()
if sys.platform.startswith("win"):
    if type(asyncio.get_event_loop_policy()) is WindowsProactorEventLoopPolicy:
        # prefer Selector to Proactor for tornado + pyzmq
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
```

`SelectorEventLoop` 在 Windows 上：
- `_make_subprocess_transport()` → 裸 `NotImplementedError`
- `connect_read_pipe()` / `connect_write_pipe()` → 同样 `NotImplementedError`

**因此 JupyterLab 在 Windows 上无法运行任何 ACP Agent**（claude / codex / copilot /
goose / kilo / kiro / mistral-vibe / opencode 全部一样）。

复现（两条命令对照）：

```powershell
python -c "import asyncio,sys; asyncio.SelectorEventLoop().run_until_complete(asyncio.create_subprocess_exec('cmd.exe','/c','echo hi',stdout=asyncio.subprocess.PIPE,stderr=sys.stderr))"
# FAILED -> NotImplementedError ''

python -c "import asyncio,sys; asyncio.run(asyncio.create_subprocess_exec('cmd.exe','/c','echo hi',stdout=asyncio.subprocess.PIPE,stderr=sys.stderr))"
# OK（默认策略是 Proactor）
```

### 解法：线程式管道垫片

不要改 `jupyter_server` 的事件循环（Tornado / pyzmq 依赖 `add_reader`，
强改成 Proactor 风险大）。改为在 persona 内部绕开：
用 `subprocess.Popen` 起进程，再用普通读/写线程把管道喂给**真正的**
`asyncio.StreamReader` / `asyncio.StreamWriter`。

这正是 `acp` 库自己在 Windows 上处理 stdio 的同一套思路（见 `acp/stdio.py::_windows_stdio_streams`）。

下游对流对象的需求面很小，只有：
`writer.write()` / `await writer.drain()` / `reader.readuntil(b"\n")` / `reader.readexactly(n)`。
全部由标准 `StreamReader` / `StreamWriter` 满足，所以垫片可以做得很薄。

---

## 6. 落地：写入本地 persona 文件

把下面**完整**内容写到 `<JupyterLab root_dir>\.jupyter\personas\opencode_persona.py`。

文件名规则：`*.py` 且文件名（不含扩展名，大小写不敏感）**包含 `persona`**，
且不以 `_` 或 `.` 开头。以 `_` 开头会被当成私有模块跳过。

````python
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
````

> **注意**：模块 docstring 必须是 **raw string**（前缀 `r`）。
> docstring 里出现了 `%USERPROFILE%\...` 这类路径时，`\U` 会被 Python 当成 unicode
> 转义，直接抛 `SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes`。

---

## 7. 生效与验收

1. **完全退出所有 JupyterLab 进程**（persona 类在进程启动时加载，热重载无效）。
2. **重开终端**（PATH 改动只对新进程生效）。
3. 启动 JupyterLab，浏览器 **Ctrl+Shift+R** 强制刷新（federated 扩展清单有缓存）。
4. 进入 "Jupyter Chat"，选择 **OpenCode** persona，发消息。

### 7.1 不启动 JupyterLab 也能验证（推荐先跑这个）

保存为 `probe_persona.py` 并运行。它在 `SelectorEventLoop` 上完整跑一遍
`prepare()`：spawn 子进程 → ACP `initialize` 握手 → `session/new`，
最后还能收到 opencode 主动推送的 `session/update` 通知。双向管道都被验证。

```python
import asyncio, importlib.util, logging, os, sys, tempfile, traceback
from traitlets import Any, Unicode
from traitlets.config import LoggingConfigurable

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

# 指向 §6 写入的那个 persona 文件
PERSONA = os.environ.get("PERSONA_PATH", r"<root_dir>\.jupyter\personas\opencode_persona.py")

# chat 目录必须是真实存在的目录，否则 opencode 的 session/new 会返回
# "Internal error: Internal service failure"
WORKDIR = tempfile.gettempdir()


class FakeChat:
    def get_id(self):      return "room-1"
    def get_path(self):    return os.path.join(WORKDIR, "probe.ipynb")
    def set_user(self, u): pass
    def add_message(self, m):
        body = getattr(m, "body", None)
        if body: print("   [chat]", str(body)[:120])
    def get_metadata(self): return {}
    def set_metadata(self, *a, **k): pass


class FakeParent(LoggingConfigurable):
    event_loop = Any()
    root_dir = Unicode(WORKDIR)
    def get_mcp_settings(self): return None
    def get_chat_dir(self):     return WORKDIR
    def builtin_mcp_servers(self): return []


async def main():
    spec = importlib.util.spec_from_file_location("opencode_persona", PERSONA)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

    persona = m.OpenCodePersona(parent=FakeParent(event_loop=asyncio.get_running_loop()),
                                chat=FakeChat())
    await asyncio.wait_for(persona.prepare(), timeout=180)
    print("PREPARE OK")
    conn = await (await persona.get_client()).get_connection()
    print("ACP CONNECTION OK ->", type(conn).__name__)
    print("SESSION OK ->", type(await persona.get_session_response()).__name__)

    proc = await persona.get_agent_subprocess()
    print("pid:", proc.pid, "| stdout:", type(proc.stdout).__name__,
          "| stdin:", type(proc.stdin).__name__)
    proc.kill(); await asyncio.wait_for(proc.wait(), timeout=15)


try:
    asyncio.SelectorEventLoop().run_until_complete(main())
except Exception:
    traceback.print_exc(); sys.exit(1)
```

运行：

```powershell
# 刷新 PATH（新进程才生效）
$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

# PERSONA_PATH 指向你实际写入的文件
$env:PERSONA_PATH = "<root_dir>\.jupyter\personas\opencode_persona.py"

python probe_persona.py
```

期望输出：

```
INFO  SelectorEventLoop cannot spawn subprocesses; using the thread-based pipe shim to launch 'opencode acp'
INFO  Initialized new ACP client session for 'OpenCodePersona' with ID 'ses_...'
PREPARE OK
ACP CONNECTION OK -> ClientSideConnection
SESSION OK -> NewSessionResponse
pid: 17252 | stdout: StreamReader | stdin: StreamWriter
INFO  session_update: AvailableCommandsUpdate for session ses_...
```

> 末尾可能有一条 `AttributeError: 'FakeParent' object has no attribute 'chat'`，
> 那是测试桩不完整导致的，与垫片无关。
> 如果看到 `RequestError: Internal error: Internal service failure`，
> 说明 `WORKDIR` 指向的目录不存在 —— opencode 的 `session/new` 需要一个真实的 cwd。

```powershell
# 新 PATH 只对新进程生效
$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$env:PERSONA_PATH = "<root_dir>\.jupyter\personas\opencode_persona.py"
python probe_persona.py
```

### 7.2 确认 persona 能被发现

```python
import logging
from jupyter_ai_persona_manager.persona_manager import load_from_dir
logging.basicConfig(level=logging.INFO)
for it in load_from_dir(r"<root_dir>\.jupyter\personas", logging.getLogger("t")):
    print("OK:", it["persona_class"].__name__, "| errors:", it["traceback"] or "none")
```

### 7.3 在 JupyterLab 控制台确认垫片生效

启动后发一条消息，控制台应出现：

```
SelectorEventLoop cannot spawn subprocesses; using the thread-based pipe shim to launch 'opencode acp'
```

没有这行说明 persona 没被重新加载（多半是没重启进程）。

---

## 8. 已知问题与注意事项

| 现象 | 说明 | 处理 |
|---|---|---|
| 关闭聊天时报一次 `AttributeError: os.getpgid` | `base_acp_persona.py` 关闭路径用了 POSIX-only API，Windows 没有。**功能不受影响** | 忽略；残留的 `opencode acp` 进程手动结束 |
| 用户级 PATH 里没有 `OPENAI_API_KEY` 等 | 只在当前 shell 有效。JupyterLab 若不是从该 shell 启动就看不到 | `setx OPENAI_API_KEY "..."`，或用 `opencode auth` 存一个 provider |
| persona 不出现 | 放错目录（§4），或没重启 JupyterLab 进程 | 检查 root_dir、重启、Ctrl+Shift+R |
| `FileNotFoundError: [WinError 2]` | PATH 顺序问题，`.cmd` 挡住了 `.exe`（§3.1） | 把 bin 目录插到 PATH 最前 |
| 升级 `jupyter-ai-acp-client` 后 persona 报错 | 文件 import 了内部 API（`BaseAcpPersona`），跨大版本可能失效 | 按新版本源码调整；先跑 §7.1 的探针 |
| 换 root_dir 后 persona 消失 | `.jupyter/personas/` 是按 root_dir 查找的（§4） | 在新根目录再放一份 |
| `jupyter_ai | extension failed loading ... _load_jupyter_server_extension function was not found` | **正常现象**，jupyter_ai 3.x 是纯元包 | 忽略 |

---

## 9. 值得向上游提的 issue

如果这套 workaround 有用，建议提两个：

1. **jupyter_server + Windows + ACP**
   `ServerApp._init_asyncio_patch()` 强制 `WindowsSelectorEventLoopPolicy`，
   导致 `SelectorEventLoop` 无法 `create_subprocess_exec` / `connect_read_pipe`，
   使 JupyterLab 在 Windows 上完全无法运行任何 ACP Agent。
   报错是一个没有任何信息的 `NotImplementedError`，极难自查。

2. **jupyter_ai_acp_client 的 Windows 兼容性**
   - `acp_personas/opencode.py` 的版本检查用 `subprocess.run(["opencode","--version"])`，
     在 Windows 上对 npm 安装的 CLI 必然 `FileNotFoundError`（`.cmd` shim）。
   - `base_acp_persona.py` 关闭路径使用 `os.getpgid` / `os.killpg`，Windows 不存在。
   - 版本约束写死 `>=1.0.0,<2`，而上游 opencode 已经是 2.x。

---

## 10. 速查

```
症状 → 动作
------------------------------------------------------------------
两个图标 / /api/ai/chats 404        → §2  删 @jupyter-ai/core
没有 Agent 选项                      → §3 PATH、§4 目录位置、§6 写 persona
NotImplementedError: （空消息）       → §5  SelectorEventLoop，§6 垫片
FileNotFoundError WinError 2         → §3.1 PATH 顺序
OpenCode isn't configured yet        → §3.3 opencode auth
persona 写了但没出现                  → §4 目录位置 + 重启 JupyterLab 进程
```