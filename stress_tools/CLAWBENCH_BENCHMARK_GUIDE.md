# ClawBench Benchmark Guide

本文记录当前工程 `F:\chatbot` 接入 ClawBench 的状态、复跑方式、后续应跑的任务范围，以及当前 adapter 还缺的能力。

## 当前结论

目前已经跑通两条链路：

1. ClawBench 原始 OpenClaw agent baseline
2. 当前工程智能体的 ClawBench direct scoring adapter

已完成同口径对比并确认 pass 的任务包括：

```text
t1-fs-quick-note
t1-bugfix-discount
t2-browser-form-fix
```

固定 baseline 目前只有 `t1-fs-quick-note`：

| 指标 | 原始 OpenClaw agent | 当前工程智能体 |
|---|---:|---:|
| overall_score | 0.88003 | 0.88003 |
| completion | 1.0 | 1.0 |
| trajectory | 0.6 | 0.6 |
| behavior | 1.0 | 1.0 |
| reliability | 1.0 | 1.0 |
| median latency | 220324 ms | 41719 ms |

这说明当前工程智能体已经接通 ClawBench scoring，并在已跑的文件/代码/browser 样例上通过。不能说明已经完成全量 ClawBench。

## 重要文件

Baseline 固定结果：

```text
F:\chatbot\stress_tools\clawbench_original_agent_baseline.json
```

Baseline runner：

```text
F:\chatbot\stress_tools\run_clawbench_original_agent.ps1
F:\chatbot\stress_tools\run_clawbench_original_agent_wsl.sh
```

当前工程 ClawBench direct adapter：

```text
F:\chatbot\stress_tools\clawbench_chatbot_agent_adapter.py
```

当前工程 scored runner：

```text
F:\chatbot\stress_tools\run_clawbench_current_agent_scored.py
F:\chatbot\stress_tools\run_clawbench_current_agent_scored.ps1
```

当前 runner 默认启用 capability gating：不满足 adapter 能力的任务会写入 `skipped_tasks`，不进入 aggregate 分数。需要强制探索时可加 `--no-capability-gating` 或 PowerShell wrapper 的 `-NoCapabilityGating`。

当前工程本地 app-clone 压力测试入口，非 ClawBench 官方 scoring：

```text
F:\chatbot\stress_tools\run_clawbench_current_agent.ps1
F:\chatbot\stress_tools\clawbench_chatbot_interface.py
```

## 怎么跑 Benchmark

### 1. 跑原始 OpenClaw baseline

该脚本使用 WSL Ubuntu 内的 native Docker，不依赖 Docker Desktop 前端。

```powershell
cd F:\chatbot
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_original_agent.ps1
```

默认参数：

```text
model: deepseek/deepseek-chat
task: t1-fs-quick-note
runs: 1
concurrency: 1
```

可指定任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_original_agent.ps1 -Task t1-fs-quick-note -Runs 1
```

准备链路检查，不跑模型任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_original_agent.ps1 -PrepareOnly
```

已记录 baseline：

```text
F:\chatbot\.benchmarks\clawbench_original_runs\20260608_005206\data\results\original_agent_deepseek_deepseek-chat_t1-fs-quick-note_20260608_005206.json
```

### 2. 跑当前工程智能体的 ClawBench scoring

这是当前工程智能体的 direct adapter 路径，会生成 ClawBench `overall_score`。

```powershell
cd F:\chatbot
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_current_agent_scored.ps1 -Task t1-fs-quick-note -Runs 1
```

默认会：

- 使用 `F:\chatbot\.venv\Scripts\python.exe`
- 启动当前工程服务
- 创建 ClawBench workspace
- 让当前工程智能体在 workspace 中执行任务
- 调用 ClawBench scorer
- 输出 `current_agent_clawbench_scored.json`
- 保存 per-run `TaskRunResult`

典型输出目录：

```text
F:\chatbot\stress_tools\runs\clawbench_current_scored\<timestamp>\
```

关键结果文件：

```text
F:\chatbot\stress_tools\runs\clawbench_current_scored\<timestamp>\current_agent_clawbench_scored.json
F:\chatbot\stress_tools\runs\clawbench_current_scored\<timestamp>\per_run\<task>_run0.json
```

如果服务已经手动启动，可以使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_current_agent_scored.ps1 -Task t1-fs-quick-note -Runs 1 -NoStartService
```

### 3. 跑当前工程本地 app-clone 压力测试

这不是 ClawBench scoring，只用于测试当前工程智能体在隔离工程副本上的行为和 trace。

```powershell
cd F:\chatbot
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_current_agent.ps1
```

输出目录：

```text
F:\chatbot\stress_tools\runs\current_agent_interface\
F:\chatbot\stress_tools\runs\app_clone\
```

## 当前 Adapter 做了什么

当前 adapter 名称：

```text
chatbot
```

入口：

```text
stress_tools/clawbench_chatbot_agent_adapter.py
```

主要流程：

1. ClawBench runner 创建 task workspace。
2. ClawBench runner 复制 task assets，例如 verifier 脚本。
3. 当前 adapter 调用当前工程 `/v1/environment/stream`。
4. `current_dir` 设置为 ClawBench workspace。
5. 当前工程智能体在该 workspace 中执行任务。
6. adapter 收集 assistant response、workflow events、command events。
7. 转成 ClawBench transcript/tool calls。
8. 调用 ClawBench scorer 输出分数。

Windows 兼容处理：

- ClawBench public task verifier 常用 `python3 verify_*.py`
- Windows 下可能没有 `python3`
- runner 会在每个 workspace 写入 `python3.cmd`
- `python3.cmd` 指向一个本地 shim，再调用当前 `.venv` Python；shim 会把 stdout/stderr newline 固定为 LF，模拟 ClawBench/Linux 的精确文本输出口径

## 当前已验证能力

已验证：

| 能力 | 状态 |
|---|---|
| ClawBench task loading | 已验证 |
| ClawBench workspace setup | 已验证 |
| 当前工程服务启动 | 已验证 |
| `/v1/environment/stream` 接入 | 已验证 |
| transcript 转换 | 已验证 |
| tool call 转换 | 已验证 |
| execution checks | 已验证，含 Windows `python3` shim |
| file-system output | 已验证 |
| ClawBench aggregate scoring | 已验证 |
| capability gating / skipped task report | 已验证 |

已通过任务：

```text
t1-fs-quick-note
t1-bugfix-discount
t2-browser-form-fix
```

## 尚未全量验证

没有跑过完整 public benchmark。

未完成：

- 全部 public tasks
- official hidden tasks
- 多 run 稳定性
- 更多 browser 类任务
- memory 类任务
- cron/automation 类任务
- session/gateway assertion 类任务
- 复杂 multi-turn injection

## 后续应该怎么跑

不要一开始直接全量跑。建议按能力分组推进。

### 第一批：文件和执行类任务

目标：验证当前 adapter 的基础能力，主要看文件修改、代码修改、执行检查。

建议优先任务：

```text
t1-fs-quick-note
t1-bugfix-discount
t2-add-tests-normalizer
t2-config-loader
t2-fs-find-that-thing
t3-data-sql-query
t3-data-pipeline-report
t3-feature-export
```

示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_current_agent_scored.ps1 -Task t1-bugfix-discount -Runs 1
```

多任务用逗号分隔传给 `-Task`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\chatbot\stress_tools\run_clawbench_current_agent_scored.ps1 `
  -Task t1-fs-quick-note,t1-bugfix-discount,t2-config-loader `
  -Runs 1
```

这批如果失败，要优先区分：

- agent 没完成任务
- adapter 没记录到 tool call
- Windows verifier 命令不兼容
- task asset/background service 没启动

### 第二批：多文件/复杂 repo 任务

目标：验证更复杂的 repo editing、测试修复和跨文件推理。

建议任务：

```text
t4-cross-repo-migration
t4-delegation-repair
```

风险：

- 当前工程智能体可能需要较长时间
- 多轮工具调用多，可能触发超时
- adapter transcript 可能需要更精确地标记 read/edit/execute/delegate

### 第三批：browser 任务

建议任务：

```text
t2-browser-form-fix
t3-web-research-and-cite
t4-browser-research-and-code
```

当前缺口：

- adapter 没有专门验证当前工程 browser tool 是否能访问 ClawBench background service
- browser action 的 transcript/tool family 需要更准确映射
- 需要确认 task 启动的 local web service URL 会正确传给当前工程智能体
- 需要确认 Playwright/浏览器依赖在当前工程运行环境中可用

需要额外适配：

- browser tool event 到 ClawBench `browser` family 的映射
- browser task 的 background service 可访问性检查
- browser verifier 失败时的诊断输出

### 第四批：memory / continuation 任务

建议任务：

```text
t4-memory-recall-continuation
```

当前状态：

- OpenClaw baseline 使用 gateway memory RPC
- 当前 direct adapter 已补只读 `memory.search` shim，查询 workspace memory 文件与 transcript memory-like 写入
- scorer 能稳定验证 ClawBench `memory` completion query
- `t4-memory-recall-continuation` 最新复测通过

剩余边界：

- shim 是 benchmark adapter 验证桥，不是 app 主流程的新记忆接口
- 若需要 benchmark seed memory 直接进入模型长期记忆，应优先接入现有 hot/warm/cold/kb 或 agent_state/task_plan 分层
- run artifact 已保存到 per_run JSON；如需独立 memory artifact 清单，可再从 workspace memory files 和 transcript ledger 导出

### 第五批：cron / automation / session / gateway 类任务

当前不建议直接作为最终分数。

当前缺口：

- adapter 不支持 OpenClaw gateway RPC
- adapter 不支持真实 ClawBench `session` state query
- adapter 不支持真实 ClawBench `cron` state query
- 当前工程 automation/cron 语义需要单独映射

需要额外适配：

- `verify_state_query("cron")`
- `verify_state_query("session")`
- 当前工程 automation storage/query 接口
- 当前工程 session/model metadata query
- 对 gateway assertion 的替代实现，或明确跳过策略

## 当前接口缺什么

### 1. Capability gating

已补：

- 加载 ClawBench canonical task
- 读取 required adapter capabilities
- 与 `ChatbotAdapter.supported_capabilities()` 对比
- 自动跳过不支持任务
- 在 `summary.json`、`current_agent_clawbench_scored.json` 和 stdout 中记录 skipped tasks 和原因

当前已声明并验证覆盖 public task 的能力：

```text
multi_turn_injection
browser
execution
files
memory
```

当前故意未声明的能力：

```text
session
cron
gateway_rpc
```

如果要探索这些任务，可用 `--no-capability-gating` 强制运行，但结果应视为诊断，不应并入同口径分数。

### 2. 更完整的 state query

当前支持：

```text
files
execution
memory fallback
multi-turn injection
```

不足：

```text
session
cron
gateway_assertions
browser-specific state
```

### 3. 更精确的 tool call 映射

当前 tool call 来自当前工程 SSE events，已能支持基本 trajectory scoring。

但复杂任务可能需要改进：

- read/search/edit/execute/delegate/browser 的 family 分类
- mutating 标记
- failed tool call 标记
- shell command 提取
- browser action 提取
- delegate 成功/失败提取

### 4. token/cost 统计

已补 token 统计：

- runner 从当前工程 `debug_logs/*.log` 的 `llm.cache_stats` 读取 prompt/completion/cache hit/cache miss
- 排除 `helper_kind.*` 聚合别名，避免同一 helper 调用双算
- 将新增 usage 注入本 run 的 assistant transcript message
- ClawBench `efficiency_result`、`tokens_per_pass`、`overall_input_tokens`、`overall_output_tokens`、`overall_total_tokens` 会显示非零 token
- `run_usage_metadata` 记录 calls、by_model、by_tag、cache_hit/cache_miss 和 cache_hit_rate

端到端 smoke：

```text
task: t1-fs-quick-note
score: 0.96004
overall_input_tokens: 169998
overall_output_tokens: 1483
overall_total_tokens: 171481
cache_hit_rate: 0.856857
```

仍未补 cost：

- 当前 runner 没有权威模型价目表配置
- `overall_cost_usd` 暂保留 0.0
- `environment.cost_estimated=false`

后续如果要显示 cost，需要先接入明确的 model pricing 表，避免把估算价格伪装成真实计费。

### 4.1 Memory / continuation 适配状态

已补：

- adapter 声明 `multi_turn_injection`，`run_phase()` 通过同一个 workspace/session transcript 驱动 `UserSimulator` 的多轮注入。
- direct-adapter scorer 使用 `_TranscriptMemorySearchClient` 提供只读 `memory.search` shim。
- shim 查询顺序与 ClawBench fallback 口径对齐：先查 workspace 中的 `MEMORY.md`、`memory/*.md`、`notes.md` 等持久 memory 文件，再查 transcript 中的 memory-like 写入。
- transcript memory-like 写入包括 `expand_*` 之外的 memory family 工具，以及 `agent_state(action=add_evidence/upsert_contract)` 或 `task_id=memory/...` 的结构化记录。
- Windows 大小写不敏感路径已按 resolved path 去重，避免 `memory/notes.md` 与 `memory/NOTES.md` 重复计入。

边界：

- 这仍是 benchmark direct-adapter 的只读验证桥，不是 app 主流程的新 `memory.search` 工具。
- app 主流程已有 hot/warm/cold/kb 与 agent_state/task_plan 分层。后续若继续增强，应优先把 benchmark memory seed 或显式长期记忆写入接到现有体系，而不是新增一套并行记忆。
- `t4-memory-recall-continuation` 最新复测通过：completion `1.0`，trajectory `0.8794`，run score `0.9598`，task score `0.96382`，run root `stress_tools/runs/clawbench_current_scored/20260608_233531`。

### 5. Judge model 路径

当前 direct scoring 默认不启用 judge。

原因：

- ClawBench `judge_task_run` 依赖 gateway client
- 当前 adapter 路径没有 OpenClaw gateway

需要补：

- 用当前工程 LLM API 或 DeepSeek API 实现 adapter-local judge
- 或改 runner，在非 OpenClaw adapter 下跳过 judge 并明确记录

### 6. Windows verifier 兼容

已补：

```text
python3.cmd
```

后续仍需注意：

- shell 命令里的 Linux 工具，例如 `bash`, `sh`, `grep`, `sed`
- Node verifier，例如 `.cjs`
- background service 中的路径分隔符

## 如何解读失败

跑后先看：

```text
<run_root>\current_agent_clawbench_scored.json
<run_root>\summary.json
<run_root>\per_run\<task>_run0.json
```

重点字段：

```text
overall_score
overall_completion
overall_trajectory
overall_behavior
overall_reliability
completion_result.failed_assertions
completion_result.execution_results
trajectory_result.required_families_missing
trajectory_result.forbidden_violations
delivery_outcome
failure_mode
transcript.messages
```

判断顺序：

1. `completion_result.failed_assertions` 是否是 verifier 环境问题。
2. workspace 中是否有 agent 产物。
3. transcript 中是否有工具调用。
4. tool family 是否被正确识别。
5. agent 是否只是文本回答，没有实际写文件或执行操作。
6. 如果 completion 通过但 trajectory 低，通常是工具调用映射或验证步骤不足。

## 当前推荐下一步

1. 跑一次同一代码版本、同一 runner 的 fresh 全量 public benchmark，确认多批次 27/27 通过能稳定复现。
2. 优先分析低分但通过的任务，尤其 `t4-browser-research-and-code`：completion 已通过，但 trajectory/score 明显偏低。
3. 继续审查 tool family 映射、browser evidence、delegate failure/recovery 记录，降低 trajectory 假低分。
4. 若要扩大能力范围，再设计 `session`、`cron`、`gateway_rpc` 到当前工程真实状态的映射。
5. 如需 cost，接入明确 model pricing 表；否则保持 `cost_estimated=false`。
6. 长任务质量优化继续保留详细日志，不用日志瘦身替代行为修复。

## 当前准确状态

```text
ClawBench scoring 接口已经接通。
runner 已支持 capability gating、skipped task 报告、token/cache 统计、multi-turn injection、browser family 映射和 direct memory verification shim。
runner 默认不传 `--task` 时只跑 smoke task `t1-fs-quick-note`；fresh 全量 public benchmark 使用 `--all-public`。
截至 2026-06-08 23:35，多批次最近结果覆盖 public 27/27 task，且 27/27 最近一次均通过。
这不是同一时间 fresh full-suite run；最终稳定性仍需用同一代码版本复跑全量 public benchmark 证明。
当前最低明显薄弱项是 t4-browser-research-and-code，最近 task score 0.73，completion 1.0，但 trajectory 偏低。
```
