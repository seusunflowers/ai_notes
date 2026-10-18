# TermMate OpenCode v2 补丁包

配套手册：`docs/SUBLIME_TEXT_TERMMATE_OPENCODEV2_SUPPORT.md`（**§0.2 一页复现**；§6 补丁原理、§7 覆盖安装、§8 验证、§12 回滚）。

本目录保存可重复执行的修复脚本、验证工具与回滚备份，用于 Package Control 更新覆盖后快速复原。

## 验证记录

- 日期：2026-10-17
- 环境：opencode CLI **2.0.32**（Windows npm 安装，本机在 `C:\nvm4w\...\opencode.exe`）、Sublime Text 4 build 4215（插件宿主 Python 3.14）、TermMate **0.6.1**
- 验证模型：`deepseek/deepseek-flash`（另用 `deepseek/deepseek-v4-pro` 复核过模型选择与 thinking 事件）；失败路径模型：`ollama/llama3:8b`
- 验证结果：`tm_verify_v2.py` **32/32 PASS**（见 `logs/tm_verify_v2.out.txt`）
- 交叉复核：上一轮的独立脚本 `%TEMP%\tm_verify_2032.py`（26 条断言，含真实 reasoning 事件与合成 `session.tool.failed`）
  对同一文件同样 **26/26 PASS**
- 最终生效的适配器快照：`backups/opencode_agent.py.patched.verified`（LF 84,351 字节，`D5425D81…28BBFF`），与本机
  `%APPDATA%\Sublime Text\Packages\TermMate\genfoundry\opencode_agent.py` 逐字节一致
- 上一轮记录（保留作对照）：2026-10-16，opencode **2.0.30**，**29/29 PASS**。那一版快照的 `rewind()` 走
  `DELETE /revert`，在 2.0.32 上已失效

### 相对 2.0.30 的两处改动

1. **`rewind` 改用 staged pair。** `DELETE /revert?messageID=` 换成
   `POST …/revert/stage {"messageID"}` + `POST …/revert/commit`。2.0.32 上 DELETE 返回 204 但**不改变任何消息**
   （实测不但不减，反而多出一条：6 → 7），只有 staged pair 才按消息粒度截断（实测 6 → 2）。旧式 DELETE 仅保留为 404/405 回退。
   语义：目标消息本身也会被删除——与 TermMate 的 rewind 按钮一致（截断后把该条 prompt 原文放回输入框）。
2. **验证脚本的 rewind 断言收紧。** 从"调用没抛异常"改为"消息数确实减少、目标消息确实消失、剩余集合与预期完全一致"。
   旧断言正是让上面这个 bug 通过 29/29 的原因：`rewind("msg_dummy")` 只是不抛异常而已。

## 文件清单

文本文件按 **LF** 统计字节数与 SHA-256（`core.autocrlf=true`：git 里的规范形式是 LF，多数文件在 Windows 工作区被检出成 CRLF，
所以磁盘字节数会更大）；`backups/opencode_agent.py.patched.verified` 特意保持 LF——它与线上安装的适配器逐字节相同。
`TermMate.sublime-package.disabled.060` 是二进制 zip，按原样统计。

| 文件 | 大小 (B) | SHA-256 |
|---|---|---|
| `tm_patch.py` | 32153 | `10DC9DB2981826CD6F9A2DFE543CBC6B9549709A4E761730359FF8FC44F7F824` |
| `tm_verify_v2.py` | 14037 | `E7CE008E646479BE633B20164877B9B12DA52DEBB6467C9441D17E8DC3500ACE` |
| `tm_probe_v2.py` | 11811 | `DF2B2F83537F5DF0CC7EA35C44FE1D09A3833755BBBBBC1F4175A0FB3A370500` |
| `check_syntax.py` | 128 | `CFCF76AE01E095347508A5043E838AFA2DA6F4C7EB6D3A3A18B5CC8F67B993DC` |
| `logs/tm_probe_v2.out.txt` | 25361 | `CC55688B9E6109F6AF9758B76F2347F37110EECB9229FB65652C5C0A4D23A559` |
| `logs/tm_verify_v2.out.txt` | 3346 | `7EAC5D66911968AB90F7DBBB5486AEBDDBB9A6C803A97C6D7EEC4C2B58B81B09` |
| `backups/opencode_agent.py.orig.060` | 67773 | `00F1849755503CB560361DEB3C406F4C86AD9F801448AB24F67808D8D24A2F10` |
| `backups/opencode_agent.py.pre_script.bak` | 68081 | `11EF79EA0B074359AFDEC328EC01F311940AFC8B306A3D0BFDE51E2612D45C19` |
| `backups/opencode_agent.py.patched.verified` | 84351 | `D5425D817BDED7C52BC638B68945234DE0A2B9DC92788712A4827DB1E428BBFF` |
| `backups/TermMate.sublime-package.disabled.060` | 505751 | `5F2D8F67ACEEC22FDCBDCF9118D73FF5F3D99B2624ED76163B5ADB94D6C426AE` |

## 使用方法

1. **重新打补丁**（Package Control 更新覆盖后）：
   - 按手册 §7 把 `Installed Packages\TermMate.sublime-package` 移出并解包到 `Packages\TermMate`；
   - 运行 `python tm_patch.py`。输入可以是刚解包的**原始**文件（§6.1 横幅正则已并入脚本），
     也可以是当年已含 §6.1 的旧状态（`pre_script.bak`）——两条路径输出逐字节相同，已实测；
   - 脚本幂等：已打过补丁时输出 `No changes needed`，不写文件；锚点计数不是 1 时**不写文件**并报错；
   - 重启 Sublime Text（必须——插件宿主仅在启动时加载该模块）。
   目标默认是 `%APPDATA%\Sublime Text\Packages\TermMate\genfoundry\opencode_agent.py`，可用 `TM_TARGET` / `TM_BACKUP` 覆盖。
2. **验证**：
   ```powershell
   $env:TM_CLI = "C:\nvm4w\nodejs\node_modules\@opencode\cli\bin\opencode.exe"
   $env:TM_MODEL = "deepseek/deepseek-flash"
   $env:TM_FAIL_MODEL = "ollama/llama3:8b"
   python tm_verify_v2.py                  # 默认测已安装的包
   $env:TM_PKG = "<...>\genfoundry"        # 或测任意待验适配器所在目录（需同时有 base_agent.py）
   ```
   需要可用的 opencode CLI 与模型凭据；退出时自动清理自己启动的服务进程（`taskkill /PID <pid> /T /F`）。
3. **语法检查**：`python check_syntax.py <文件>`。
4. **还原**：用 `backups\` 覆盖对应位置——
   - `orig.060` = 上游原始适配器（0.6.0 与 0.6.1 的 `opencode_agent.py` 逐字节相同，故这一个备份两版通用）；
   - `pre_script.bak` = 原始 + 手册 §6.1 手工小改（历史遗留，现在只作对照，不再是脚本的必需输入）；
   - `patched.verified` = 本目录脚本产出的已验证状态（v2.0.32）。

## 注意

- 脚本内路径全部由 `%APPDATA%` / `%TEMP%` 推导，另可用 `TM_TARGET` / `TM_BACKUP` / `TM_PKG` / `TM_CLI` / `TM_MODEL` /
  `TM_FAIL_MODEL` 覆盖；跨机器无需改代码。
- 日志已脱敏：本机路径替换为 `%TEMP%` / `%APPDATA%` / `%HOME%` / `%OPENCODE_CLI%` 占位符，墙钟时间替换为 `%TODAY%`；
  一次性服务口令只记录"是否捕获到"（`password_captured=True`），不记录口令本身。
- `tm_patch.py` 的 29 个 edit 里**包含 §6.1**（横幅正则 + `_SERVER_PASSWORD_RE` / `_is_v2_banner`）。旧版把这一步留给手工，
  于是"解包后直接跑脚本"会漏掉 §6.1，产出的文件里 `_SERVER_PASSWORD_RE` 只有引用没有定义。
- `tm_patch.py` 修复过一处幂等缺陷：旧版对已补丁文件重复执行会把 4 个"插入式"修改重复应用（方法重复定义）。
  现版本重复执行是安全的 no-op。
- 本仓库 `core.autocrlf=true`：用 `git apply` 打上面的补丁时，git 会把结果改写成 CRLF（内容对、哈希不对），
  要逐字节一致请加 `-c core.autocrlf=false`。`tm_patch.py` 不受影响——它按输入文件的行尾风格回写
  （LF 进、LF 出；CRLF 进、CRLF 出）。
- Package Control 的 TermMate 更新会覆盖补丁（手册 §11/§12）——更新后重跑本目录流程即可恢复。
- **重跑 `tm_patch.py` 覆盖 `opencode_agent.py` 时，如果 Sublime Text 还在运行**：插件重载会随着 Job Object 一起结束每个会话自己的
  `opencode serve`，已打开的会话随后只会报 `WinError 10061`。先打补丁再重启，或事后用 `TermMate: Clear Session` 重置会话
  （手册 §9.6；本机 2026-10-17 实际踩到过一次）。
- `tm_probe_v2.py` 是当初用来重新探测 v2 线路格式的一次性脚本，保留作参考；`logs/tm_probe_v2.out.txt`
  是 **2.0.30** 那次的探测记录（未随本轮刷新）。无参数直接运行即可，退出时自动清理自己启动的服务进程。
- 本机被移出加载路径的档案是 0.6.1：`%TEMP%\TermMate.sublime-package.disabled.061`（506,001 字节，
  `26DCEC7129D1D81B0DAC7E53F084B252217D195C8041B1883CCCDD40238A50D6`）。仓库里的
  `disabled.060` 是 0.6.0 的档案：两者 `opencode_agent.py` 相同，包内其他文件略有差异。
