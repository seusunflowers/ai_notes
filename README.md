# ai_notes

跨机器复用的系统配置与排障笔记。目标是「在一台 Windows 机器上踩过的坑，
换台机器能让 agent 按文档直接修好」，而不是记录流水账。

## 目录结构

```
ai_notes/
├── docs/        排障与安装手册，面向 agent 编写
│   ├── JUPYTER_AI_OPENCODE_WINDOWS.md
│   └── SUBLIME_TEXT_TERMMATE_OPENCODEV2_SUPPORT.md
└── personas/    可直接复制的配置片段（规范源）
    └── opencode_persona.py
```

手册清单：

| 手册 | 解决什么 |
|---|---|
| `JUPYTER_AI_OPENCODE_WINDOWS.md` | Windows 上安装 JupyterLab / Jupyter AI，并把 OpenCode、Claude、Codex 等 ACP Agent 接成聊天后端 |
| `SUBLIME_TEXT_TERMMATE_OPENCODEV2_SUPPORT.md` | Sublime Text 4 的 TermMate 包接不上 OpenCode v2：模型列表为空、无输出。含 v1→v2 API 差异对照表、事件映射、补丁代码、不依赖 Sublime 的验证脚本，以及覆盖归档的安装与回滚步骤 |

- `docs/` 里的手册遵循同一套结构：症状→根因对照表 → 环境探测命令 →
  按序修复步骤（命令可直接粘贴）→ 内联完整源码 → 不依赖服务的验证探针 →
  已知问题表 → 速查表。
- `personas/` 存放**规范源**。实际生效的副本要复制到使用方的目标目录
  （见对应手册的说明），不要直接在这里改了就以为生效了。

## 使用方式

克隆到任意位置，路径自定。下文用 `<repo-root>` 指代仓库根目录——**具体路径由每台机器自己决定，不要从本仓库推断**：

```powershell
git clone <repo-url> <repo-root>
```

新机器上手后，让 agent 读仓库内 `docs/` 下对应手册执行。

### 让 opencode 自动发现

两个文件都要改，缺一不可：

1. `references` 让 opencode 把 `docs/` 挂成可访问的**目录**；
2. 全局 `AGENTS.md` 里的指针让 agent 知道**什么时候该去读**。

只做第 1 步，agent 能访问目录但不知道手册存在；只做第 2 步，agent 会去读一个它无权访问的路径。

#### 1. 注册目录：`~/.config/opencode/opencode.json`

把下面这段**合并进**已有的 `references` 块（该文件还有 `model`、`mcp`、`plugin` 等其他键，
不要整份覆盖）：

```jsonc
{
  "references": {
    "ai_notes": {
      "path": "<repo-root>/docs",
      "description": "跨机器的系统配置与排障手册。涉及 JupyterLab / Jupyter AI / Sublime Text TermMate / Windows 环境时查阅"
    }
  }
}
```

`<repo-root>` 必须换成**该机器上**仓库的实际绝对路径（例如本机为 `E:/LLM/ai_notes/docs`）。
opencode 会把它渲染成注入给 agent 的 reference 列表——路径写错，agent 就找不到手册，
会按「文档不存在」处理。所以这是全仓库唯一允许出现绝对路径的地方。

改完重开 opencode 会话，确认注入里出现：

```
<available_references>
  <reference>
    <name>ai_notes</name>
    <path>...</path>
```

#### 2. 加指针：`~/.config/opencode/AGENTS.md`

在「排障手册」表格里加一行，指向仓库内的手册文件（用相对仓库的路径）：

```markdown
## 排障手册

跨机器复用的系统配置与排障手册放在 `<repo-root>/docs/`。命中的时候去读对应文件，
不要凭记忆重写命令。

| 手册 | 什么时候读 |
|---|---|
| `<repo-root>/docs/JUPYTER_AI_OPENCODE_WINDOWS.md` | 在 Windows 上安装或排查 JupyterLab / Jupyter AI；要用 OpenCode、Claude、Codex 等 ACP Agent 作为 Jupyter 聊天后端时 |
| `<repo-root>/docs/SUBLIME_TEXT_TERMMATE_OPENCODEV2_SUPPORT.md` | Sublime Text 4 的 TermMate 包接不上 OpenCode v2（模型列表为空、无输出）；或要给其他机器复刻这个修复 |
```

每新增一份 `docs/` 下的手册，就在这里补一行——AGENTS.md 是常驻注入的，
手册不在表里就等于不存在。

绝对路径只写在 opencode 的配置里，**不写进本仓库**，这样仓库可以放在任何位置。

## 隐私

本仓库面向公开分享，已按以下规则脱敏：

- 不出现任何真实用户名、邮箱、主机名、组织名
- 不出现真实笔记本/代码目录（统一用 `<root_dir>` 占位）
- Windows 路径一律使用 `%APPDATA%`、`%USERPROFILE%` 等环境变量，或写成占位符
- 仓库内只使用相对路径；机器相关路径放在各自的 opencode 配置里

提交前可用 `git grep -nEi "Users\\\\|@gmail|@qq|@163|[A-Z]:\\\\Users" `
自查是否引入了新的标识信息。

## 写作约定

- **不写机器专属路径**。需要路径时用探测命令现取，或写成占位符 + 说明怎么替换。
- **每条结论都要有可复现的验证方式**，不能只写「这样就好了」。
- **踩坑记录要写清楚错误信息原文**。空消息的异常尤其重要——
  `NotImplementedError:` 后面什么都没有，正是当时绕了远路的最大原因。
- **内联的代码要真的跑过**。写完把代码块抽出来执行一遍，别交付跑不通的示例。
- **标注正常现象**。哪些警告日志是无关的（例如 `jupyter_ai | extension failed loading`），
  明确写出来能省掉后续大量排查。