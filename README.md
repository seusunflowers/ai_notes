# ai_notes

跨机器复用的系统配置与排障笔记。目标是「在一台 Windows 机器上踩过的坑，
换台机器能让 agent 按文档直接修好」，而不是记录流水账。

## 目录结构

```
ai_notes/
├── docs/        排障与安装手册，面向 agent 编写
│   └── JUPYTER_AI_OPENCODE_WINDOWS.md
└── personas/    可直接复制的配置片段（规范源）
    └── opencode_persona.py
```

- `docs/` 里的手册遵循同一套结构：症状→根因对照表 → 环境探测命令 →
  按序修复步骤（命令可直接粘贴）→ 内联完整源码 → 不依赖服务的验证探针 →
  已知问题表 → 速查表。
- `personas/` 存放**规范源**。实际生效的副本要复制到使用方的目标目录
  （见对应手册的说明），不要直接在这里改了就以为生效了。

## 使用方式

克隆到任意位置（路径自定，本机放在 `D:\ai_notes`）：

```powershell
git clone <repo-url> D:\ai_notes
```

新机器上手后，让 agent 读仓库内 `docs/` 下对应手册执行。

### 让 opencode 自动发现

在 `~/.config/opencode/opencode.json` 里注册本仓库的 `docs/` 目录，
并在全局 `~/.config/opencode/AGENTS.md` 里加一行指针：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "references": {
    "ai_notes": {
      "path": "D:/ai_notes/docs",
      "description": "跨机器的系统配置与排障手册。涉及 JupyterLab / Jupyter AI / Windows 环境时查阅"
    }
  }
}
```

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