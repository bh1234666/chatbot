# Environment 模式修改方案

## 目标

新增 `environment` 特殊模式，用于 Codex / Claude Code 类工程协作场景。

要求：

- 原 `/v1/chat/stream`、QQ bridge、现有 prompt、现有工具行为完全等价。
- 原模式下模型不能看到 environment 提示词、environment 工具、目录参数。
- 不复制两份基础 prompt。基础 prompt 仍只有一份，environment 只作为 addon 注入。
- 不改变现有 orchestrator 三轮流程，只在模式为 environment 时增加上下文与工具。
- 外部工程目录不能被模型直接编辑。所有源码修改走严格流程：
  `env_fetch -> workspace 编辑副本 -> env_diff -> env_apply_replace/create`。
- 允许在外部工程目录运行命令，但运行与文件回写分离，避免目录混乱。

## 总体设计

### 模式识别

新增运行时 ContextVar：

```python
current_runtime_mode = ContextVar("runtime_mode", default="chat")
current_environment = ContextVar("environment", default=None)
```

默认值是 `chat`，所以旧 API 不设置时行为完全不变。

environment route 在调用 `orchestrate()` 前设置：

```python
mode = "environment"
environment = {
    "root_dir": normalized_current_dir,
    "project_id": project_id,
    "group_id": env_user_group_id,
    "archive_id": project_archive_id,
}
```

请求结束后 reset token。

## 身份映射

不改底层记忆模型。

```text
一个用户 = 一个 group
一个用户项目 = 一个 archive
```

映射：

```text
group_id   = env_user:{user_id}
archive_id = arch_env_{hash(user_id + normalized_current_dir)}
user_id    = 请求传入 user_id
```

同一用户不同项目共享同一个 `group_id`，因此群组级记忆有一定互通；不同项目使用不同 `archive_id`，项目上下文隔离。

如果 `archive_id` 不存在，environment route 自动创建或 upsert archive/persona。

### 可落地修正：archive 映射不要直接伪造 ID

当前 `archive.create_archive()` 会生成 `arch_<ULID>`，直接构造 `arch_env_<hash>` 需要改 DAO 和迁移，收益不大。更稳妥的第一版做法：

- `group_id` 仍可确定性生成：`env_user:{safe_user_id}`。
- `project_key = sha256(user_id + normalized_current_dir)[:16]`。
- 用一个轻量映射保存 `project_key -> archive_id`。

映射存储优先级：

1. 第一版推荐 JSON 文件：`data/environment_projects.json`。
   - 本地前端单机使用足够。
   - 不改 memory 表，不影响旧 API。
   - 写入用 atomic replace，避免半文件。
2. 后续如需要多 worker / 多进程，再迁移到 DB 表 `environment_projects`。

JSON 结构：

```json
{
  "projects": {
    "user_id:project_key": {
      "user_id": "u1",
      "project_key": "abc123",
      "archive_id": "arch_...",
      "group_id": "env_user:u1",
      "root_dir": "F:/project",
      "project_name": "project",
      "created_at": "...",
      "last_seen_at": "..."
    }
  }
}
```

创建流程：

1. 查映射。
2. 映射存在且 archive 存在 → 复用。
3. 映射缺失或 archive 被删 → 调 `create_archive(name=f"env:{user_id}:{project_name}")`。
4. 写入映射。
5. upsert environment persona。

这样保留现有 archive 机制，同时实现项目稳定复用。

## 新 API

新增文件：

```text
app/api/environment.py
```

新增请求 schema：

```python
class EnvironmentChatRequest(BaseModel):
    user_id: str
    user_name: str = ""
    message: str
    current_dir: str
    project_id: str = ""
    persona_id: str = "environment"
    client_msg_id: str | None = None
```

新增接口：

```text
POST /v1/environment/stream
```

处理流程：

1. normalize `current_dir`。
2. 校验目录存在且是目录。
3. 生成 `group_id/archive_id`。
4. 确保 archive 存在。
5. 设置 ContextVar。
6. 构造普通 `ChatRequest`。
7. 调用现有 `orchestrate()`。
8. SSE 事件格式保持与 `/v1/chat/stream` 一致。

旧 `/v1/chat/stream` 不改行为。

### API 可行性说明

`/v1/environment/stream` 不应复制 `/v1/chat/stream` 的完整实现。建议抽取或复用现有 chat stream 的公共部分：

- idempotency 可先复用 `client_msg_id` 逻辑；若短期抽取成本高，environment route 可先独立实现一个最小幂等缓存。
- group guard 仍用现有 `(archive_id, group_id, user_id)` 锁。
- active archive 校验不应套用 QQ group 的 `bot_config.get_active_archive()` 逻辑；environment route 自己解析 archive 后直接调用 orchestrate。
- 文件下载第一版仍可用 `/v1/chat/files/{archive_id}/{group_id}/...` 下载 workspace 产物；工程文件本身不通过该接口下载，前端用 env tools 获取内容。

## Prompt 注入方案

不复制基础 prompt。

新增一个 addon 构造函数：

```python
def build_mode_addon(mode: str) -> str:
    if mode == "environment":
        return ENVIRONMENT_PROMPT_ADDON
    return ""
```

在现有基础 prompt 构建完成后追加：

```python
addon = build_mode_addon(current_runtime_mode.get())
if addon:
    append_to_dynamic_session_info(addon)
```

关键点：

- `chat` 模式返回空字符串。
- 原基础提示词仍只维护一份。
- environment 规则只作为附加规则存在。
- 后续原基础提示词优化会同时对两个模式生效。

environment addon 内容要强调：

- 当前 workspace 不是工程目录。
- 工程真实结构只以 env tools 为准。
- 不得用 `commit_to_main/promote_to_main` 回写工程。
- 修改工程文件必须：
  1. `env_fetch`
  2. workspace 内编辑副本
  3. `env_diff`
  4. `env_apply_replace` 或 `env_apply_create`
- `env_run` 可用于测试/构建，但源码级写入仍必须走 apply 流程。

### Prompt 注入位置约束

为了避免破坏 prefix cache 和旧 prompt 维护方式，environment addon 不应复制或改写基础 prompt。推荐注入到现有“动态会话信息”通道：

- chat 模式：addon 为空，不产生额外 message。
- environment 模式：作为额外规则块加入最后的动态 user/system 注入区。

如果当前 round2 和 helper prompt 分别有独立构建函数，则只加入“同一个 addon 文本引用”，不要写两份规则：

```python
ENVIRONMENT_PROMPT_ADDON = "..."

def environment_prompt_addon() -> str:
    if current_runtime_mode.get() != "environment":
        return ""
    return ENVIRONMENT_PROMPT_ADDON
```

主线程、helper 如都需要看到规则，也都引用该函数。禁止在多个文件手写重复版本。

## 工具注入方案

当前 `ROUND2_TOOLS` 是全局工具列表，不能直接加入 env 工具，否则 QQ 模式会看到。

新增函数：

```python
def tools_for_runtime_mode(mode: str) -> list[dict]:
    if mode == "environment":
        return ROUND2_TOOLS + ENVIRONMENT_TOOLS
    return ROUND2_TOOLS
```

原模式下返回原对象或等价列表，保证工具集合完全一致。

所有调用 LLM tool schema 的地方从：

```python
ROUND2_TOOLS
```

改为：

```python
tools_for_runtime_mode(current_runtime_mode.get())
```

这是必要小改，但不改变流程。

### 工具注入可行性

当前风险点是 `ROUND2_TOOLS` 可能在多个地方直接传入 LLM。实现前需要先 `rg "ROUND2_TOOLS|MAIN_THREAD_TOOLS"`，把调用点收敛成一个函数：

```python
def current_round2_tools() -> list[dict]:
    return tools_for_runtime_mode(current_runtime_mode.get())
```

旧模式等价要求：

- `current_round2_tools()` 在 chat 模式返回的工具 name 顺序与旧 `ROUND2_TOOLS` 完全一致。
- 不在模块 import 时修改 `ROUND2_TOOLS`。
- environment 工具 schema 不 append 到全局 `ROUND2_TOOLS`，只在函数返回时拼接。

dispatch 层可以识别 env 工具，但如果 `current_environment` 为空，必须拒绝执行：

```json
{"ok": false, "error": "environment_context_required"}
```

这样即使旧模式模型幻觉调用 env 工具，也不会生效。

## Environment 工具

新增文件：

```text
app/llm/tools/environment.py
app/llm/tools/environment_schemas.py
```

### env_list_tree

查看工程目录结构。

参数：

```json
{
  "path": ".",
  "max_depth": 3,
  "max_entries": 300
}
```

限制：

- `path` 必须在 `root_dir` 内。
- 默认过滤 `.git/node_modules/__pycache__/dist/build/.venv` 等大目录。
- 返回树形结构和截断信息。

### env_read

读取工程目录文件。

```json
{
  "path": "src/main.py",
  "offset": 0,
  "limit": 20000
}
```

只读，不写入 workspace。

### env_search

在工程目录搜索。

```json
{
  "pattern": "class Foo",
  "path": ".",
  "file_glob": "*.py",
  "is_regex": false,
  "max_results": 100
}
```

用于真实工程结构搜索，不依赖 workspace。

### env_fetch

从工程目录复制文件到 workspace。

```json
{
  "paths": ["src/main.py", "pyproject.toml"]
}
```

行为：

- 复制到 workspace 的 `_env/` 子目录。
- 保留相对路径，如 `_env/src/main.py`。
- 写入 manifest：
  - env path
  - workspace path
  - original sha256
  - mtime
  - size

### env_diff

对比 workspace 副本和外部原文件。

```json
{
  "path": "src/main.py"
}
```

或：

```json
{
  "workspace_path": "_env/src/main.py",
  "target_path": "src/main.py"
}
```

返回 unified diff 摘要；长 diff 写入 workspace 文本文件。

### env_apply_replace

替换已有工程文件。

```json
{
  "workspace_path": "_env/src/main.py",
  "target_path": "src/main.py"
}
```

限制：

- 默认只允许替换 manifest 中 `env_fetch` 过的文件。
- apply 前校验原文件 hash 是否仍等于 fetch 时 hash。
- 若外部文件已被用户改动，拒绝并要求重新 fetch/diff。
- 不允许目录级复制。
- 不允许根据 deliverables 自动提升。

### env_apply_create

新建单文件。

```json
{
  "workspace_path": "_env/src/new_file.py",
  "target_path": "src/new_file.py"
}
```

限制：

- 必须显式 `target_path`。
- 目标文件已存在则拒绝。
- 不允许创建到 root 外。
- 不允许批量目录创建，除非每个文件显式列出。

### env_run

在工程目录下执行命令。

```json
{
  "cwd": ".",
  "command": "pytest -q",
  "timeout_sec": 120,
  "mode": "read_only|build|mutate"
}
```

建议策略：

- 默认允许 `read_only` 和 `build`。
- `mutate` 必须显式声明。
- 禁止危险命令：
  - `rm -rf`
  - `del /s`
  - `format`
  - `git reset --hard`
  - `git clean -fdx`
  - 明显覆盖写入重定向
- 命令输出过长时写入 workspace 日志文件。
- 不自动把命令产生的文件纳入回写流程。
- 运行后如检测到工程文件变化，返回 changed files 提醒模型使用 `env_diff`。

### 工具性能边界

为避免本地大工程卡死，所有 env 工具必须有硬边界：

- `env_list_tree`
  - 默认忽略：`.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, `__pycache__`, `.cache`。
  - 默认 `max_depth=3`, `max_entries=300`。
  - 返回 `truncated=true` 和 ignored dirs 统计。
- `env_read`
  - 默认最多读取 20KB。
  - 大文件只返回片段，并提示用 offset 继续读。
  - 二进制文件拒绝或只返回元数据。
- `env_search`
  - 优先调用 `rg`。
  - 限制 `max_files/max_results/max_bytes`。
  - 搜索大目录时应用同一 ignore 规则。
- `env_fetch`
  - 单文件默认上限，例如 5MB；超限需要显式 `allow_large=true`。
  - 批量文件数量默认上限，例如 50。
- `env_diff`
  - 大 diff 写入 workspace 文件，只返回摘要。
- `env_run`
  - stdout/stderr ring buffer 有固定大小。
  - SSE 输出节流，例如 250ms 或 4KB 一次，避免刷爆前端。
  - timeout 必填或有默认上限。

## Manifest

每个 environment 会话 workspace 内维护：

```text
.env_session_manifest.json
```

结构：

```json
{
  "root_dir": "F:/project",
  "fetched_files": {
    "src/main.py": {
      "workspace_path": "_env/src/main.py",
      "sha256": "...",
      "mtime": 123456,
      "size": 1234
    }
  },
  "applied_files": [],
  "created_files": []
}
```

作用：

- 防止模型乱 apply。
- 防止外部文件被用户改动后覆盖。
- 审计本轮改了什么。
- 不依赖混乱的主 workspace 文件结构。

### Manifest 性能与一致性

`env_fetch` 只对被 fetch 的文件计算 hash，不扫描整个工程。

`env_apply_replace` 校验：

- 目标文件仍存在。
- 目标文件当前 sha256 等于 fetch 时 sha256。
- workspace 文件存在且在 workspace root 内。
- target path 在 root_dir 内。

`env_run` 后的 changed files 检测：

- 如果是 git repo：优先用 `git status --porcelain`，性能最好。
- 如果不是 git repo：第一版只检测 manifest 中 fetched 文件的 hash/mtime 是否变化，不全盘扫描。
- 不把 build/cache 产物纳入 manifest。

这样可避免大工程每轮全量 hash。

## 与现有 workspace 的关系

environment 模式下：

- workspace 仍用于临时编辑、helper 沙箱、日志、产物。
- 外部工程目录不是 workspace。
- `commit_to_main` 仍然只影响 chatbot workspace，不代表工程回写。
- 工程回写只能走 `env_apply_replace/create`。
- 不使用 `promote_to_main` 作为工程目录同步机制。

### 避免当前主工作区混乱的具体策略

environment 模式不依赖主工作区文件结构来判断工程状态。

- 工程结构来自 `env_list_tree/env_search`。
- 工程文件内容来自 `env_read/env_fetch`。
- 工程回写来自 `env_apply_*`。
- workspace 中 `_env/` 只是副本区，不代表完整工程树。
- `deliverables` 只代表给用户看的附件，不代表要写回工程。

即使现有主工作区混乱，也不会污染外部工程目录。

## 原模式等价保证

### chat 模式下

- `current_runtime_mode` 默认是 `chat`。
- `current_environment` 默认是 `None`。
- `tools_for_runtime_mode("chat")` 返回现有工具集合。
- `build_mode_addon("chat")` 返回空字符串。
- `/v1/chat/stream` 不设置 environment context。
- QQ bridge 不传新字段。
- 模型看不到 env 工具和 env prompt。

### 测试要求

新增回归测试：

1. `chat` 模式工具列表与旧 `ROUND2_TOOLS` 名称完全一致。
2. `chat` 模式 base prompt 不包含：
   - `env_fetch`
   - `env_apply`
   - `current_dir`
   - `工程目录`
3. `/v1/chat/stream` 构造的请求不会设置 environment context。
4. `/v1/environment/stream` 才能看到 env 工具。
5. env 工具路径逃逸测试：
   - `../`
   - 绝对路径
   - symlink escape
6. apply hash 冲突测试。
7. apply 不允许目录级 promote。
8. env_run cwd 逃逸拒绝。
9. env_run 危险命令拒绝。

## 本地前端流式工作流、监控与打断

QQ bridge 只需要最终输出和少量进度提示；本地 environment 前端需要完整观察工作流，并能随时打断。该能力应作为 environment route 的增强层实现，不改变旧 `/v1/chat/stream` 语义。

### 与现有架构的取舍

目标不是重写一套 Codex，而是在现有 agent 架构上补齐本地工程模式最关键的能力。已有能力应优先复用：

- 现有三轮流程继续负责意图判断、工具规划、最终回复。
- 现有 `delegate/helper/processes` 继续负责长任务、并行任务、冻结、恢复、资源 helper。
- 现有 workspace 继续作为沙箱、helper 工作区、临时产物区。
- 现有 dream/maintenance/summary 已有丰富后台整理能力，虽然结构不同于 Codex 的 run store，但不影响根本使用，且改动量巨大，暂时不重构。
- 新增 environment 只补“外部工程目录访问、受控回写、本地前端实时观察、细粒度打断”。

因此，近期不引入完整独立的 `Run/Step` 数据库模型作为硬依赖。先用现有 trace、helper ledger、process registry、debug/event sink 补齐前端可见状态；如果后续前端恢复能力不足，再逐步把事件和步骤持久化。

### 能合并到原始架构的增强

以下能力不应只服务 environment，适合合并到原有架构，QQ 模式可以不展示但后端可受益：

- 更完善的工具调用事件：在 `registry.dispatch()` 统一发出 tool start/done/error，environment 前端消费，旧模式 no-op。
- 更完善的 helper 报告机制：helper 完成时结构化输出 terminal_reason、outputs_complete、quality_warnings、resource_required、files。
- 更完善的 processes/active 查询：统一展示活跃 helper、命令、长工具状态。
- 更完善的恢复机制：复用 pause_state、completion ledger、process registry；先做同进程恢复与当前活跃状态查询，不立即上完整持久 run DB。
- 更丰富工具可以统一纳入工具体系，但通过 mode-aware schema 控制可见性，旧模式不暴露 environment 专属工具。

### 必须通过 environment addon 实现的能力

以下能力与 QQ/chat 语义不同，不应合并进普通模式：

- 外部工程目录参数 `current_dir/root_dir`。
- `env_list/env_read/env_search/env_fetch/env_diff/env_apply/env_run` 工具。
- “workspace 不是工程目录”的约束。
- “不能直接编辑工程目录，必须 fetch/diff/apply”的流程。
- 本地前端的回写确认、命令取消、工程 diff 展示。

### 暂缓的大改

以下能力有价值，但会显著改变当前系统，暂缓：

- 独立 Codex 风格 `Run/Step/Event` 持久数据库替换现有 trace/debug/ledger。
- 重构 dream/maintenance 为 run-aware 后台整理。
- 重构 memory 为 user-level/project-level 多层画像。
- 重构 orchestrator 为非聊天式 task engine。

这些不影响第一版 environment 的核心可用性。

### 事件流目标

`/v1/environment/stream` 继续使用 SSE，但事件粒度更完整。旧 orchestrator 已有 `meta/progress/token/done/complete/error`，environment 模式在不改变主流程的前提下补充更多结构化事件：

```text
meta                 会话创建，返回 trace_id、archive_id、group_id、project_id、root_dir
workflow_start       工作流开始，包含 runtime_mode=environment
round_start          round1/round2/round3 开始
round_done           round1/round2/round3 完成摘要
tool_start           工具调用开始
tool_progress        长工具进度，例如 env_run stdout tail、helper 心跳
tool_done            工具调用结束，包含 ok、elapsed、摘要
helper_spawned       helper 创建
helper_update        helper 状态更新、心跳、最近工具
helper_done          helper 完成、失败、冻结或被中断
env_diff_ready       env_diff 结果可读，长 diff 给 workspace 日志路径
env_apply_ready      可回写候选，需要前端/用户确认时发送
env_apply_done       回写完成
command_output       env_run 输出增量或压缩 tail
interrupt_ack        打断请求已接收
token                最终自然语言输出 token
done                 最终回复完成
complete             会话完全结束、锁释放前后状态
error                错误
```

兼容策略：

- 旧事件不删、不改字段含义。
- environment 前端消费新增事件；旧 QQ bridge 忽略未知事件即可。
- 如果当前 EventSourceResponse 封装只透传 `orchestrate()` 事件，可以在 environment route 包一层 event mapper，把 debug/progress/tool 状态转换成前端事件。

### SSE 合并实现建议

environment route 需要同时转发：

- `orchestrate()` 产生的原始事件。
- environment event sink 产生的工具/命令/helper 事件。

推荐实现：

1. route 创建 `asyncio.Queue` 作为 event sink。
2. 设置 ContextVar，让工具和 registry 能 `emit(queue_event)`。
3. 后台 task 运行 orchestrate，把原始事件也写入同一个 queue。
4. SSE generator 从 queue 读取并 yield。
5. orchestrate 结束且 queue drain 后发送 complete。

这样可以实时输出 env_run stdout，而不必等 tool 调用结束。

注意：

- queue 要有最大长度，防止前端断开时内存无限增长。
- 前端断开时取消 orchestrate task，并触发 conversation abort 清理命令。
- chat route 不使用这套 queue，旧行为不变。

### 实时汇报补强

当前最大短板是“用户只能看到最终 round3 汇报，过程细节不足”。environment 模式需要实时汇报，但不要求模型额外写大量自然语言。推荐后端事件驱动：

- round 切换时自动发 `round_start/round_done`。
- 工具调用时自动发 `tool_start/tool_done`。
- helper 有心跳或状态变化时自动发 `helper_update`。
- env_run 有 stdout/stderr 时自动发 `command_output`。
- 关键等待点发 `waiting`，例如等待 helper、等待命令、等待用户确认。
- 最终 round3 仍负责自然语言总结，不承担过程直播。

同时可把这套机制部分合并到原始架构：

- 普通 chat 仍只发送原有 progress，不展示详细工具事件。
- environment route 开启详细事件。
- 后端统一 emit，前端是否展示由 route/mode 决定。

这样不会让 QQ 输出变啰嗦，也不需要两套工具执行逻辑。

### 前端可见工作流状态

本地前端应能展示：

- 当前阶段：加载记忆、round1、round2、helper 执行、round3、维护。
- 当前活跃工具：工具名、参数摘要、开始时间、运行时长。
- 活跃 helper：task_id、kind、mode、状态、最近心跳、已产出文件、是否 frozen/resource_required。
- env_run 命令：cwd、command、pid、运行时长、stdout/stderr tail、退出码。
- env 文件变更：fetched、modified in workspace、diff ready、applied、conflict。
- 可打断对象：
  - 整个会话
  - 某个 helper
  - 某个 env_run 命令
  - 仅停止继续生成最终文本

### 轻量运行状态模型

不立即新增完整 run/step 数据库，但 environment 后端仍应维护一个轻量运行状态，供 SSE 和 active 查询使用。

建议以内存 registry + 可选 JSONL 事件日志实现：

```json
{
  "trace_id": "...",
  "runtime_mode": "environment",
  "archive_id": "...",
  "group_id": "...",
  "user_id": "...",
  "root_dir": "F:/project",
  "status": "running|completed|interrupted|failed",
  "current_phase": "round2",
  "active_tools": [],
  "active_helpers": [],
  "active_commands": [],
  "pending_confirmations": [],
  "applied_files": [],
  "pending_workspace_changes": []
}
```

这不是新主流程，只是观测索引：

- 来源复用现有 trace、debug、helper ledger、process registry、env command registry。
- 服务重启后的强恢复可后续再做；第一版保证同进程前端刷新可查询。
- 若后续需要持久恢复，再把 JSONL 事件日志升级为 DB。

### 轻量状态的边界

第一版轻量状态只保证同进程可观测，不承诺服务重启后恢复正在运行的任务。

必须持久化的只有：

- `.env_session_manifest.json`
- env diff 文件
- env command 全量日志
- 已 apply 文件记录

这些文件放在 workspace 内，便于最终回复和后续排查。

### 打断接口

保留旧 `/v1/chat/abort`。为 environment 增加更细粒度接口：

```text
POST /v1/environment/abort
```

请求：

```json
{
  "trace_id": "optional",
  "archive_id": "optional",
  "group_id": "optional",
  "user_id": "u1",
  "scope": "conversation|helper|command|generation",
  "task_id": "optional helper task_id",
  "command_id": "optional env_run command id",
  "reason": "user requested"
}
```

语义：

- `conversation`：中断整个会话，类似旧 abort，但返回更完整的 `interrupt_ack` 和当前活跃对象摘要。
- `helper`：协作中断指定 helper，走现有 delegate kill/abort 语义，尽量让 helper 写进展报告。
- `command`：终止指定 `env_run` 子进程。Windows 下需要 process group / job object 或 `taskkill /T`。
- `generation`：停止后续 LLM 输出，不一定杀已完成工具结果。

返回：

```json
{
  "ok": true,
  "trace_id": "...",
  "interrupted": ["conversation"],
  "active_helpers": [],
  "active_commands": []
}
```

### env_run 进程监控与取消

`env_run` 不能只是 `subprocess.run()` 阻塞等待。需要可监控、可取消：

- 每次 `env_run` 分配 `command_id`。
- 进程注册到进程 registry，记录：
  - trace_id
  - archive_id/group_id/user_id
  - root_dir/cwd
  - command
  - pid/process group
  - started_at
  - stdout/stderr ring buffer
  - status
- stdout/stderr 异步读取，定期通过 SSE 发 `command_output` 或 `tool_progress`。
- 输出过长时：
  - 前端只收 tail 和累计字节数。
  - 全量输出写入 workspace 日志文件。
- timeout 到期时发送 `tool_done ok=false error=timeout`。
- 用户打断 command 时：
  - 先 graceful terminate。
  - 超时后 kill process tree。
  - 返回 `interrupted=true`，不把部分输出当成功。

### env_run 安全策略优化

命令安全不要只靠字符串黑名单，但第一版可以使用“黑名单 + 模式声明 + 变更检测”的组合。

推荐：

- `mode=read_only`
  - 禁止明显写入符号：`>`, `>>`, `2>`, `&& echo`, `Out-File`, `Set-Content`。
  - 允许 `git status`, `git diff`, `rg`, `dir`, `ls`, `pytest --collect-only` 等。
- `mode=build`
  - 允许测试、构建、lint。
  - 允许缓存和 build artifacts。
  - 不承诺无写入，但写入只能是工具链副产物。
- `mode=mutate`
  - 允许安装依赖、格式化、代码生成。
  - 默认进入 confirmation 或至少高亮风险。
  - 运行后必须返回 changed files。

无论什么模式，危险命令始终拒绝：

- `git reset --hard`
- `git clean -fdx`
- `rm -rf /`
- `del /s /q`
- `format`
- 直接删除 root_dir 或其父目录

PowerShell/cmd/bash 语法差异较大，第一版可保守拒绝复杂链式命令；用户确实需要时再放宽。

### env_run 与现有工具链关系

现有 workspace `bash/run` 仍用于沙箱内操作，不直接替代 `env_run`。

`env_run` 专门用于真实工程目录：

- 运行测试、构建、lint、git status/diff。
- 可产生构建缓存，但不允许把源码修改作为隐式成果。
- 如果命令导致文件变更，返回 changed files，要求模型再用 `env_diff/env_apply` 或解释变更来源。

后端可复用现有 workspace_run 的输出截断、超时、命令摘要代码，但 process registry、cwd 安全、工程变更扫描要 environment 专用。

### helper 监控

现有 delegate 已有 helper 状态、wait_window、processes 工具和 heartbeat。environment 前端需要把这些变成主动流式事件：

- helper spawn 时发送 `helper_spawned`。
- wait_window 内每隔固定时间或状态变化时发送 `helper_update`。
- helper 结束发送 `helper_done`，包含：
  - terminal_reason
  - outputs_complete
  - quality_warnings
  - resource_required
  - files
  - elapsed_sec

实现上优先复用现有 `delegate.progress`、`process registry`、`debug.log` 位置，不复制 helper 调度逻辑。

### 恢复机制补强

当前已有：

- pause_state
- helper completion ledger
- process registry
- debug trace
- workspace manifest

第一版 environment 恢复应复用这些能力，而不是新建完整恢复系统：

- `/v1/environment/active` 汇总当前活跃会话、helper、command。
- `/v1/environment/runs/{trace_id}` 返回轻量运行状态。
- 如果 helper 已完成但 pending result 丢失，继续复用 completion ledger / disk result 恢复。
- 如果前端刷新，重新订阅 SSE 时可先查 active，再继续显示后续事件。
- 如果整个服务重启，暂时只保证 workspace 和 manifest 保留，不保证自动恢复正在运行的命令。

可合并到原架构的补强：

- processes 查询输出更结构化。
- helper 完成结果更多写入 ledger。
- debug trace 与用户可见事件分离，避免前端直接解析 debug log。

### 报告机制补强

当前最终主要依赖 round3 自然语言汇报。environment 模式应在 complete payload 中附带结构化执行摘要，前端可直接展示：

```json
{
  "trace_id": "...",
  "runtime_mode": "environment",
  "project_changes": [
    {"path": "src/main.py", "action": "replace", "ok": true}
  ],
  "pending_changes": [
    {"path": "src/foo.py", "workspace_path": "_env/src/foo.py"}
  ],
  "commands": [
    {"command_id": "...", "command": "pytest -q", "exit_code": 0, "log_path": "..."}
  ],
  "helpers": [
    {"task_id": "...", "terminal_reason": "completed"}
  ],
  "artifacts": []
}
```

这可以合并进原始架构的 complete metadata 生成逻辑，但 chat 模式可不展示或字段为空。

### 用户确认与暂停点

environment 模式应支持前端确认，尤其是写回工程目录：

- `env_apply_replace/create` 默认可以先返回 `env_apply_ready`，由前端确认后再执行。
- 第一版如果不做确认 UI，也至少要求模型显式调用 apply；不能自动 promote。
- 后续可增加：

```text
POST /v1/environment/confirm
```

用于确认某个 pending apply 或 mutate command。

pending 结构：

```json
{
  "confirmation_id": "...",
  "kind": "apply_replace|apply_create|mutate_command",
  "summary": "...",
  "diff_path": "_env_diffs/src_main.py.diff",
  "expires_at": "..."
}
```

确认机制第一版可以分级：

- `env_apply_replace/create`：默认要求显式工具调用即可，不自动 promote；前端确认可作为后续开关。
- `env_run mode=mutate`：建议第一版进入 confirmation，除非后端配置允许自动执行。
- 删除文件、批量覆盖、依赖安装：默认 blocked，需要确认。

blocked 状态复用现有“helper 冻结/资源请求”的思想，但对象是 environment action：

```text
blocked_reason = approval_required
approval_kind = apply_replace | apply_create | mutate_command | delete_file
```

### 前端恢复与查询

本地前端可能刷新页面，需要恢复当前工作流。

新增查询接口：

```text
GET /v1/environment/runs/{trace_id}
GET /v1/environment/active?user_id=...
```

返回：

- 当前状态。
- 已发事件摘要。
- 活跃 helper。
- 活跃 command。
- 最近 token 或最终回复。
- pending confirmations。

第一版可以只实现 `active`，从现有 group guard、process registry、command registry 汇总。

### 不改变旧流程的实现方式

为了避免侵入 orchestrator 主流程：

1. 新增 `environment_event_bus` ContextVar。
2. environment route 设置 event sink。
3. env tools、delegate wrapper、env_run 在关键点向 event sink emit。
4. route 把 event sink 事件和 `orchestrate()` 事件合并成 SSE。
5. chat 模式 event sink 为空，所有 emit 是 no-op。

这样：

- 旧模式没有新增事件。
- 不需要复制 orchestrator。
- 不需要两套 prompt。
- 新增监控只在 environment mode 生效。

### 事件 emit 层级

为避免每个工具各写一套事件逻辑，建议分层：

1. `registry.dispatch()` 统一 emit 通用工具事件：
   - tool_start
   - tool_done
   - tool_error
2. env tools emit environment 专属事件：
   - env_diff_ready
   - env_apply_ready
   - env_apply_done
   - command_output
3. delegate/processes emit helper 事件：
   - helper_spawned
   - helper_update
   - helper_done
4. orchestrator 在阶段边界 emit：
   - round_start
   - round_done
   - workflow_start

chat 模式下 emit 为 no-op 或仅进入 debug，不进入用户事件流。

### 可行性评估

该方案可行，原因：

- 不替换 orchestrator，只新增 route/context/tool/event 外壳。
- 文件修改闭环由 env 工具硬保证，不依赖 prompt 自觉。
- 现有 workspace/helper/processes 已覆盖大部分 agent 执行能力。
- 旧模式默认 ContextVar 为空，不会看到新 prompt 和工具。
- 实时事件可从 dispatch/env_run/delegate 三个边界采集，不需要侵入每个业务函数。

主要工程风险：

1. `ROUND2_TOOLS` 调用点分散。
   - 缓解：先统一为 `current_round2_tools()`，加等价测试。
2. SSE 合并 orchestrate 事件和工具事件容易出现结束竞态。
   - 缓解：用单 queue + sentinel，完整测试 disconnect/abort。
3. Windows 子进程树清理复杂。
   - 缓解：第一版用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`，并在 registry finally 清理。
4. env_run mutate 命令难以完全防写。
   - 缓解：默认 blocked/confirmation，源码修改仍必须通过 apply。
5. 大工程搜索/list/hash 可能慢。
   - 缓解：默认 ignore、结果上限、只 hash fetched 文件、优先 git status。

### 打断一致性

打断后必须保持一致状态：

- 会话锁最终释放。
- env_run 子进程必须从 registry 移除。
- helper 中断结果写入 ledger 或标记 interrupted。
- 未 apply 的 workspace 修改保留，不自动写回工程。
- 已 apply 的文件记录在 manifest。
- complete 事件中返回：

```json
{
  "interrupted": true,
  "active_helpers_remaining": 0,
  "active_commands_remaining": 0,
  "workspace_preserved": true,
  "applied_files": [...]
}
```

### 相关新增测试

在前述测试基础上补充：

1. environment stream 包含 `workflow_start`，chat stream 不包含。
2. env_run 会产生 `command_output/tool_done` 事件。
3. env_run timeout 能结束并清理 registry。
4. `/v1/environment/abort scope=command` 能杀掉长命令。
5. `/v1/environment/abort scope=conversation` 会释放用户锁。
6. helper 状态事件只在 environment 模式出现。
7. 前端查询 active 能看到运行中 command/helper。
8. 打断后未 apply 的 workspace 文件不会写回工程目录。
9. chat 模式不会收到 tool_start/helper_update/command_output 等详细事件。
10. registry.dispatch 的通用 tool 事件在 environment 模式可见。

## 最小改动清单

新增：

```text
app/api/environment.py
app/llm/tools/environment.py
app/llm/tools/environment_schemas.py
app/core/runtime_mode.py
app/core/environment_events.py
app/core/environment_commands.py
app/core/environment_state.py
personas/environment.md
tests/test_environment_mode.py
tests/test_environment_tools.py
tests/test_environment_streaming.py
```

小改：

```text
app/main.py
```

注册 router。

```text
app/schemas/api.py
```

增加 `EnvironmentChatRequest`。

```text
app/llm/tools/registry.py
```

增加 mode-aware tool list，不改变现有 `ROUND2_TOOLS`。

```text
app/core/context.py 或 prompt 构建入口
```

追加 mode addon，chat 模式为空。

```text
app/core/orchestrator_entry.py
```

读取 mode-aware tool list / prompt addon。流程不变。

## 实施顺序

1. 加 `runtime_mode.py` ContextVar。
2. 加 `environment_events.py`，实现 environment-only event sink，chat 模式 no-op。
3. 加 `environment_state.py`，维护轻量 run 状态索引，复用 trace/helper/process 信息。
4. 加 project 映射 JSON store，确保 `user_id + current_dir -> archive_id` 可复用。
5. 加 environment schemas 和 router，但先不接工具。
6. 加 `tools_for_runtime_mode()`，测试 chat 模式工具等价。
7. 加 prompt addon，测试 chat 模式 prompt 无 env 内容。
8. 在 `registry.dispatch()` 接入通用 tool start/done/error 事件，chat 模式 no-op。
9. 实现 env path resolver 和 manifest。
10. 实现 `env_list_tree/env_read/env_search/env_fetch/env_diff`。
11. 实现 `env_apply_replace/env_apply_create`。
12. 实现可监控、可取消的 `env_run` 和 command registry。
13. 接入 environment tools。
14. 接入 environment SSE 增强事件。
15. 增加 environment abort/active 查询接口。
16. 增加完整回归测试。
17. 手动用 `/v1/environment/stream` 跑一个小工程验证：
    - list
    - fetch
    - edit workspace copy
    - diff
    - apply
    - run tests
    - long env_run streaming
    - command abort
    - conversation abort

## 关键结论

environment 模式不需要重构现有聊天流程。

正确边界是：

```text
旧模式 = 原流程 + 原工具 + 原 prompt
environment = 原流程 + 原工具 + env addon prompt + env tools
```

基础 prompt 仍然只有一份。environment 不复制基础 prompt，只追加模式规则，因此未来原模式 prompt 优化会自动对 environment 生效。

## 后端能力分级结论

第一版不追求完整 Codex 内核重写，而是按以下分级实现：

### 必须实现

- environment route。
- runtime mode 隔离。
- mode-aware prompt addon。
- mode-aware tools。
- env 文件访问与受控 apply。
- env_run 可监控可取消。
- environment SSE 详细事件。
- environment abort/active。
- chat 模式等价测试。

### 合并进原始架构的补强

- `registry.dispatch()` 通用工具事件。
- helper 结果结构化增强。
- processes/active 查询增强。
- completion ledger 和恢复结果增强。
- 更完善的报告机制。

这些增强即使 QQ 模式不展示，也能提高后端可诊断性。

### 暂时不做

- 完整 Run/Step DB。
- 重构 dream/maintenance。
- 重构 memory 层级。
- 重构 orchestrator 为全新 task engine。
- 前端确认 UI 的完整权限系统。

这样能在较小改动下获得接近 Codex 使用体验的核心后端能力：可观察、可打断、可运行命令、可安全修改工程文件。

## 性能评估

### LLM 成本

environment 模式不会天然增加额外 LLM 轮次：

- 仍使用原三轮流程。
- prompt 只追加一个 environment addon。
- 工具列表增加 env tools，会增加 tool schema token，但只在 environment 模式发生。
- 实时汇报由后端事件产生，不要求 LLM 额外生成过程说明。

主要 token 增量来自：

- env tool schemas。
- environment addon。
- 目录树/搜索结果。

控制方式：

- env tool schema 文案保持短而明确，不复制长教程。
- `env_list_tree/env_search` 默认截断。
- 大 diff 写文件，只给摘要。

### IO 成本

- `env_fetch` 只复制被请求文件，不镜像整个工程。
- `env_apply` 只回写显式文件。
- `env_read` 分段读取。
- `env_search` 优先 `rg`，并应用 ignore。
- hash 只针对 fetched/apply 文件。

因此大工程下的常规开销接近：

```text
O(被查看文件 + 被搜索结果 + 被修改文件)
```

而不是：

```text
O(整个工程)
```

### 事件流成本

事件流成本主要来自 env_run 输出和 helper 心跳：

- command output 需要节流。
- helper_update 只在状态变化或固定低频心跳时发送。
- event queue 需要最大长度。
- 长日志写文件，SSE 只发 tail。

建议默认：

```text
command_output interval >= 250ms
command_output chunk <= 4KB
helper_update interval >= 1s
event_queue maxsize = 1000
```

### 命令执行成本

`env_run` 性能瓶颈来自用户命令本身。后端只需保证：

- 不阻塞 event loop。
- stdout/stderr 异步读取。
- timeout 生效。
- cancel 能杀进程树。

### 恢复与状态成本

第一版轻量状态以内存为主，成本很低：

- active runs 数量通常很小。
- 每个 run 只保存摘要、ring buffer、活跃对象。
- 全量日志/diff 落盘，不进内存。

### 总体性能结论

方案性能可控。只要严格执行“不要镜像整个工程、不要全盘 hash、不要把长输出塞进 SSE/LLM 上下文”，environment 模式的额外开销主要是少量 tool schema token、受限文件 IO 和事件分发。相比 LLM 与真实构建/测试耗时，这些后端开销通常不是瓶颈。
