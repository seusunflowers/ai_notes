# 在 Windows 上让 JupyterLab 使用 OpenCode（Jupyter AI 3.x / ACP）

> 面向 agent 的操作手册。目标是让另一台 Windows 电脑上的 agent 读完这份文件后，
> 能独立诊断并修好「JupyterLab 里用不了 OpenCode」的问题。
>
> 实测环境：Windows / Python 3.14 / JupyterLab 4.6.4 / jupyter_ai 3.2.0 /
> jupyterlab-chat 0.25.1 / jupyter_ai_acp_client 0.3.3 / opencode 2.0.27

---

## 0. 先读这段：这里有四个互相独立的问题

不要试图一次修完。它们症状相似，但根因完全不同，**必须按顺序排查**。

| # | 症状 | 根因 | 章节 |
|---|---|---|---|
| 1 | 左侧有**两个**图标；点 "Jupyter AI Chat" 报 "problem with the Chat backend"；控制台反复刷 `404 GET /api/ai/chats` | jupyter_ai 升到 3.x 后，早先 2.x 的前端扩展 `@jupyter-ai/core` 变成孤儿文件，仍在请求 3.x 已删除的 `/api/ai/*` 后端 | §2 |
| 2 | "Jupyter Chat" 里**没有 Agent 选项**，或选了 Agent 也加载不出来 | persona 列表为空。8 个内置 persona 在 Windows 上全部不可用；本地 persona 又被放在了错误的目录 | §3 §4 |
| 3 | 选了 Agent，发送消息报 `NotImplementedError:`（**错误信息为空**） | jupyter_server 在 Windows 上强制 `SelectorEventLoop`，该事件循环**无法创建子进程**。这是上游缺陷，影响所有 ACP Agent | §5 |
| 4 | 发送消息报 `FileNotFoundError: [WinError 2]`（**且每次 opencode 升级后复发**） | `CreateProcess` 只补 `.exe`，不认 npm 的 `.cmd` shim。**改 PATH 顺序在 nvm4w / Program Files node 环境下无效**，见 §3.1 | §3.1 §6 |

**关键判据**：

- `NotImplementedError:` 后面什么都没有 = 事件循环问题，不要往 opencode 或 API key 方向查。
- `FileNotFoundError: [WinError 2]` = 可执行文件解析问题。先跑 §3.1 的
  `shutil.which('opencode')`，返回 `.CMD` 就是这个问题。

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

`jupyter_ai_acp_client` 用 `asyncio.create_subprocess_exec("opencode","acp")` 启动 opencode。
Windows 的 CreateProcess 在 PATH 里**只尝试补 `.exe`**，不认 `.cmd` / `.bat` / 无扩展名的
sh shim，于是：

```
FileNotFoundError: [WinError 2] 系统找不到指定的文件
```

npm 其实装了真的 exe，只是被 `.cmd` 挡住了。

#### 诊断

```powershell
# 1. which 解析到了什么？（返回 .CMD = 有问题）
python -c "import shutil; print(shutil.which('opencode'))"

# 2. npm 全局 prefix 在哪？（nvm4w 环境下会和 Machine PATH 里的目录重合）
cmd /c "npm config get prefix"

# 3. 真正的 exe 在哪
Get-ChildItem "<prefix>\node_modules\@opencode\cli\bin"

# 4. shim 里写的真实路径
Get-Content "<prefix>\opencode.cmd"
```

#### ⚠️ 改 PATH 顺序**不总是有效**

Windows 拼装进程 PATH 的顺序是 **Machine PATH ; User PATH**。所以：

- 若 npm prefix 目录**不在** Machine PATH 里（默认的 `%APPDATA%\npm` 就是），
  把它插到 User PATH 最前面就生效 —— 这是本手册早期版本的做法。
- 但用 **nvm4w**（`npm config get prefix` = `C:\nvm4w\nodejs`）或装到
  `C:\Program Files\nodejs` 时，那个目录**在 Machine PATH 里**，排在 User PATH 之前。
  **往 User PATH 最前面插是插不赢的**，`shutil.which` 永远返回 `.cmd`；
  而非管理员又改不了 Machine PATH。

```powershell
# 确认到底哪个目录赢了
python -c "import os; p=os.environ['PATH']; print([d for d in p.split(';') if d and (os.path.exists(os.path.join(d,'opencode.exe')) or os.path.exists(os.path.join(d,'opencode.cmd')))])"
# 若 'C:\nvm4w\nodejs' 排在 opencode.exe 所在目录之前 → PATH 方案无效
```

#### 正解：让 persona 自己解析 exe

§6 的 persona 带 `resolve_opencode_executable()`，按以下顺序找真正的
`opencode.exe`，完全绕开 PATH 顺序：

1. 环境变量 `OPENCODE_ACP_EXE`（显式覆盖）
2. `npm config get prefix` 报告的 prefix 下的 `node_modules\@opencode\cli\bin\opencode.exe`
3. `%APPDATA%\npm`、PATH 里的每个目录、`%ProgramFiles%\nodejs`
4. 从 `opencode.cmd` shim 文本里正则抠出被引号包住的 `opencode.exe` 路径
5. PATH 里直接存在 `opencode.exe` 的目录

多个候选时按 **mtime 最新**取胜 —— 这样升级留下的旧副本不会被误用。

可选环境变量：`OPENCODE_ACP_EXE`（直接指定 exe）、`OPENCODE_NPM_PREFIX`（跳过 `npm` 调用）。

> **为什么必须写在 persona 里**：每次 opencode 升级都可能搬家（换 prefix、换 nvm 的
> node 版本）。PATH 是机器级全局状态，改它会互相打架，也会影响别的工具；
> 而 persona 是**跟着这台机器的 Jupyter 配置走的**，解析逻辑放在那里才稳定。
> 只改文档不改代码，等于把同一个坑留给下一个 opencode 版本。

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

## 6. 落地：同步本地 persona 文件

persona 源码**不再内嵌在手册里**。规范源是本手册所在仓库的
`personas/opencode_persona.py` —— 内嵌副本必然漂移（本手册就因此在 opencode
升级后给出过过期的 PATH 结论，还差点把 §6 的说明和真实代码脱节）。

```powershell
$src = "D:\ai_notes\personas\opencode_persona.py"
$dst = "<JupyterLab root_dir>\.jupyter\personas\opencode_persona.py"

New-Item -ItemType Directory -Force -Path (Split-Path $dst)
if (Test-Path $dst) { Copy-Item $dst "$env:TEMP\opencode_persona.py.bak" -Force }
Copy-Item $src $dst -Force
Remove-Item (Join-Path (Split-Path $dst) "__pycache__") -Recurse -Force
```

改 persona 后**必须**跑一遍 §7.1 的探针再重启 JupyterLab。

文件名规则：`*.py` 且文件名（不含扩展名，大小写不敏感）**包含 `persona`**，
且不以 `_` 或 `.` 开头。以 `_` 开头会被当成私有模块跳过。

### 6.1 这个 persona 解决三件事

| 上游问题 | 手法 |
|---|---|
| 内置 `opencode-acp` persona 的版本门 `>=1.0.0,<2` | 自带本地 persona，根本不加载内置那个 |
| `FileNotFoundError: [WinError 2]`（npm `.cmd` shim） | `resolve_opencode_executable()` 自己找真实 exe，见 §3.1 |
| `NotImplementedError:`（SelectorEventLoop 起不了子进程） | `_spawn_process()`：`subprocess.Popen` + 读写线程喂给真的 `StreamReader` / `StreamWriter`，见 §5 |

### 6.2 改 persona 时的注意事项

- 模块 docstring 必须是 **raw string**（前缀 `r`）。docstring 里出现
  `%USERPROFILE%\...` 这类路径时，`\U` 会被 Python 当成 unicode 转义，直接抛
  `SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes`。
- `_init_agent_subprocess()` 里非 shim 分支要调 `super()`，它额外带了
  `limit=50MB` 和 `start_new_session=True`，别漏。
- 改完先跑 §7.1 的探针，再按 §7 重启 JupyterLab。


---

## 7. 生效与验收

1. **完全退出所有 JupyterLab 进程**（persona 类在进程启动时加载，热重载无效）。
2. **重开终端**。PATH 已不再是关键路径（见 §3.1），但如果你用了
   `OPENCODE_ACP_EXE` / `OPENCODE_NPM_PREFIX` 覆盖，它们同样只对新进程生效。
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
| `FileNotFoundError: [WinError 2]` | `.cmd` shim 挡住了 `.exe`（§3.1）。**改 PATH 顺序不一定管用** —— nvm4w / Program Files node 的 prefix 在 Machine PATH 里 | 用带 `resolve_opencode_executable()` 的 persona；或临时设 `OPENCODE_ACP_EXE=<完整 exe 路径>` |
| opencode 升级后又报 `[WinError 2]` | 升级把 exe 搬到了别处（换 prefix / 换 nvm node 版本） | persona 里的解析器按 mtime 取最新；仍失败就设 `OPENCODE_ACP_EXE` |
| `shutil.which('opencode')` 返回 `.CMD` | 正常现象，**不用修**。persona 不走 PATH | 忽略 |
| 改完文档后中文变成乱码 | PowerShell 的 `Get-Content`/`Set-Content` 用控制台代码页（cp936）读写 UTF-8 文件，字节被二次转码且不可逆 | 对本仓库文件只用 UTF-8 显式读写的工具（编辑器、`edit`、Python `read_text(encoding="utf-8")`）；误改后用 `git checkout --` 回滚 |
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
没有 Agent 选项                      → §4 目录位置、§6 同步 persona
NotImplementedError: （空消息）       → §5  SelectorEventLoop，§6 垫片
FileNotFoundError WinError 2         → §3.1  which 是 .CMD? 同步带解析器的 persona
  └ opencode 升级后复发               → §3.1  exe 搬家了，看 §8 对应行
OpenCode isn't configured yet        → §3.3 opencode auth
persona 写了但没出现                  → §4 目录位置 + 重启 JupyterLab 进程
```