# 公开快照

公开仓库：<https://github.com/bh1234666/chatbot>，默认分支 `main`，项目许可证为 MIT（见根目录 `LICENSE`）。

## 内容范围

快照包含根目录 `app/` 后端、`agent_frontend/` 静态前端、数据库迁移、相关测试、配置模板、启动入口及明确列出的维护脚本。`scripts/` 中新增的本机运维脚本必须经过审查并加入导出白名单，才能进入公开仓库。

不导出 `.env`、私有配置、数据库、聊天记录、私人账号资料、日志、前端备份和机器生成的字体缓存。启动脚本中的本机 QQ 账号会在导出副本中改为交互输入，服务地址模板会改为示例地址；源目录的运行配置不会被修改。也可以在启动前设置 `QQ_BOT_NUM`。

`packages/`、`deployments/`、`evidence/`、`harness/` 和其他独立工作区不在此快照范围内。`test_j03_wire_id_compat.py` 与 `test_sqlite_composite_benchmark_contract.py` 依赖这些本地部署或实验材料，因此保留在源目录中，不作为根目录应用的公开测试发布。

## 外部组件与许可证

MIT 许可证适用于此项目的代码。Python 依赖通过 `requirements*.txt` 声明，保留各自许可证；此项目的 MIT 声明不替代其许可条件。

NapCat、MinerU、OmniVoice、Umi-OCR、模型权重及其他第三方运行时不随快照分发。使用对应功能时需单独安装，并遵守组件和模型各自的许可证与服务条款。默认后端仅实现 SQLite，且必须使用单个 worker。

## 导出与更新

在源目录运行（输出目录应为独立的同级目录）：

```powershell
python scripts/export_public_snapshot.py --out ../chatbot-public --dry-run
python scripts/export_public_snapshot.py --out ../chatbot-public --force
```

`--force` 会刷新输出目录内容并保留 `.git/`。先确认输出仓库没有未提交的修改。导出后运行密钥扫描；发现疑似密钥或私人标识时，脚本返回非零退出码，只报告位置，不输出其值。模式扫描不能代替差异审查。

在输出仓库检查差异、运行与变更相关的测试，再提交和推送：

```powershell
git diff --stat
git status --short
git add -A
git diff --cached --check
git commit -m "upd"
git push origin main
```

Windows 的 `auto_publish.bat` 使用同一导出脚本，并在导出成功后执行提交和推送流程。

## 2026-09-16 更新检查

已检查导出文件范围、许可证声明、413 个 Python 文件的语法、密钥模式及本机私有凭据是否进入快照。相关单元测试和启动入口健康检查共 266 项：263 项通过，3 项为下述已有失败。未连接真实模型 API、QQ 或 OCR/TTS 服务进行端到端测试。

`tests/test_client_tools_loop_retry.py` 中以下三项回复文本过滤测试仍失败，在上一版公开提交 `a0d55b2` 中也可复现；本次保留其断言与失败状态，不将其计作通过：

- `test_round3_visible_protocol_guard_detects_internal_helper_structure`
- `test_round3_late_protocol_guard_uses_rolling_tail`
- `test_round3_protocol_fallback_sanitizes_internal_helper_terms`

这些失败说明部分 helper / 后台处理术语没有被既有回复过滤逻辑识别或移除，需要另行修复并验证。此次检查不代表全部测试通过或全部依赖完成许可证审计。
