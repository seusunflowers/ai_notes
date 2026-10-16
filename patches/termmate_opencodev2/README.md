# TermMate OpenCode v2 补丁包

配套手册：`docs/SUBLIME_TEXT_TERMMATE_OPENCODEV2_SUPPORT.md`（§6 补丁、§7 覆盖安装、§8 验证）。

本目录保存可重复执行的修复脚本、验证工具与回滚备份，用于 Package Control 更新覆盖后快速复原。

## 验证记录

- 日期：2026-10-16
- 环境：opencode CLI **2.0.30**（Windows npm 安装）、Sublime Text 4 build 4215（插件宿主 Python 3.14）
- 验证模型：`deepseek/deepseek-flash`；验证结果 **29/29 PASS**（见 `logs/tm_verify_v2.out.txt`）
- 最终生效的适配器快照：`backups/opencode_agent.py.patched.verified`（83,493 字节）

## 文件清单

| 文件 | 大小 (B) | SHA-256 |
|---|---|---|
| `tm_patch.py` | 30191 | `6238E737A42DCB76589EB4A6C68DAF13FB4B319EF72A6BC5274B9B2A68B2C111` |
| `tm_verify_v2.py` | 11746 | `EBC00CB3D91D8485B4C3E1A50505C68F93E32DF3EA92B47ADB0E4B5DAFAD2F92` |
| `tm_probe_v2.py` | 11811 | `DF2B2F83537F5DF0CC7EA35C44FE1D09A3833755BBBBBC1F4175A0FB3A370500` |
| `check_syntax.py` | 128 | `CFCF76AE01E095347508A5043E838AFA2DA6F4C7EB6D3A3A18B5CC8F67B993DC` |
| `logs/tm_probe_v2.out.txt` | 25361 | `CC55688B9E6109F6AF9758B76F2347F37110EECB9229FB65652C5C0A4D23A559` |
| `logs/tm_verify_v2.out.txt` | 4429 | `A1BA0F5CEBF66F1B7913F14E0A3D15B72214537A1CA552EE954840C4C037A4D0` |
| `backups/opencode_agent.py.orig.060` | 67773 | `00F1849755503CB560361DEB3C406F4C86AD9F801448AB24F67808D8D24A2F10` |
| `backups/opencode_agent.py.pre_script.bak` | 68081 | `11EF79EA0B074359AFDEC328EC01F311940AFC8B306A3D0BFDE51E2612D45C19` |
| `backups/opencode_agent.py.patched.verified` | 83493 | `55B92469A5F798A65A84032E5DCF3A97416F8BB3348B8F4EF0E8EF2F73F57551` |
| `backups/TermMate.sublime-package.disabled.060` | 505751 | `5F2D8F67ACEEC22FDCBDCF9118D73FF5F3D99B2624ED76163B5ADB94D6C426AE` |

## 使用方法

1. **重新打补丁**（Package Control 更新覆盖后）：
   - 按手册 §7 把 `Installed Packages\TermMate.sublime-package` 移出并解包到 `Packages\TermMate`；
   - 运行 `python tm_patch.py`（幂等：已打过补丁时输出 `No changes needed`，不写文件）；
   - 重启 Sublime Text（必须——插件宿主仅在启动时加载该模块）。
2. **验证**：`python tm_verify_v2.py`（需要可用的 opencode CLI 与模型凭据；可用 `TM_MODEL` / `TM_FAIL_MODEL` / `TM_CLI` 覆盖默认值）。
3. **语法检查**：`python check_syntax.py <文件>`。
4. **还原**：直接用 `backups\` 中的文件覆盖对应位置（`orig.060` = 上游原始；`pre_script.bak` = 已含手册 §6.1 手工小修改；`patched.verified` = 已验证的最终状态）。

## 注意

- 脚本内路径全部由 `%APPDATA%` / `%TEMP%` 推导，另可用 `TM_TARGET` / `TM_BACKUP` 覆盖；跨机器无需改代码。
- 日志已脱敏：本机路径替换为 `%TEMP%` / `%APPDATA%` 占位符，一次性服务口令与凭据 ID 已抹除。
- `tm_patch.py` 修复过一处幂等缺陷：旧版对已补丁文件重复执行会把 4 个"插入式"修改重复应用（方法重复定义）。现版本重复执行是安全的 no-op。
- Package Control 的 TermMate 更新会覆盖补丁（手册 §11/§12）——更新后重跑本目录流程即可恢复。
- `tm_probe_v2.py` 是当初用来重新探测 v2 线路格式的一次性脚本，保留作参考；无参数直接运行即可，退出时自动清理自己启动的服务进程。
