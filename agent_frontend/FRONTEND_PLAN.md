# bot Agent 前端计划

## 目标

为当前服务后端做一个本地前端，用作类似 Codex 的工程助手界面。对外助手名称统一叫 **bot**，不要叫 Environment。

核心能力：

- 选择/创建账号，不同账号隔离会话入口；
- 选择/切换目录路径，目录可以为空；
- 新建存档，同一用户下可以有多个存档；
- 目录归属于存档；
- 上传文件到 bot 的文件区，注意这不是设置目录，也不是直接写入项目目录；
- 发送对话内容；
- 中断任务；
- 插入对话内容，进入当前任务的 round2.5 干预；
- 加入对话到队列，当前任务结束后自动下一轮；
- 多窗口/多对话并存；
- 实时显示主进程、各级 helper、工具调用、命令输出和文件产物；
- 工作流可分级折叠。

第一阶段不重写后端调度，不复制 orchestrator。前端优先消费现有 API；只有当前 API 表达不了的行为，再列为后端补充项。

## 当前后端接口

目前主要接口是：

- `POST /v1/chat/stream`
  - `current_dir` 为空：普通聊天模式，需要传 `archive_id/group_id`。
  - `current_dir` 非空：工程 agent 模式，后端会解析目录并绑定 project/archive。
  - SSE 事件已有 `meta`、`progress`、`token`、`done`、`complete`、`error`。
  - 工程模式下还会把 environment event sink 的事件合并进同一个 SSE。

相关接口：

- `POST /v1/chat/abort`
- `POST /v1/chat/interrupt_message`
- `GET /v1/chat/active`
- `GET /v1/chat/monitor`
- `GET /v1/chat/commands/active`
- `POST /v1/chat/commands/{command_id}/abort`
- `GET /v1/chat/stage`
- `GET /v1/chat/files/{archive_id}/{group_id}/{filename}`
- `GET/POST /v1/archives`
- `GET/PUT /v1/archives/{archive_id}/persona`
- `GET /v1/personas`
- `GET/POST /v1/bot/groups...`

当前重要行为：

- `current_dir` 非空时，后端使用 `data/environment_projects.json` 建立 `(user_id, current_dir/project_id)` 到 `archive_id/group_id` 的映射。
- `current_dir` 为空时，现有 API 要求前端传明确的 `archive_id/group_id`。
- 人设文件仍叫 `personas/environment.md`，但内容已经改为 bot 身份。前端对外只显示 bot。

## 命名约定

对用户显示：

- 助手名：`bot`
- 模式名：`Agent`
- 空目录模式：`Chat`
- 有目录模式：`Project`
- 文件区：`bot 文件区`

代码内部建议：

- 前端领域模型用 `agent` 命名。
- API 兼容层可以继续传后端现有字段，例如 `current_dir`、`persona_id`。
- 不在 UI 上展示 `environment` 这个词。

## 账号模型

第一版账号只做前端本地管理：

```ts
type Account = {
  userId: string;
  displayName: string;
  createdAt: string;
  lastUsedAt: string;
};
```

前端行为：

- 左侧顶部账号切换；
- 创建账号；
- 重命名账号；
- 删除本地账号；
- 每次请求把 `userId` 作为 `user_id`，`displayName` 作为 `user_name`。

后端对应：

- 一个用户拥有一个完整群组语义；
- 同一用户的不同项目/存档属于不同个体；
- 不需要新做用户画像互通等底层改造。

## 存档、目录、文件区模型

一个用户可以有多个存档。目录路径归属于存档。目录可以为空。

```ts
type AgentArchive = {
  id: string;          // backend archive_id
  userId: string;
  title: string;
  groupId: string;
  currentDir: string;  // 可以为空
  projectId: string;
  createdAt: string;
  lastUsedAt: string;
};
```

### 空目录存档

适合把 bot 当成熟聊天机器人或通用文件处理助手使用。

建议流程：

- 前端调用 `POST /v1/archives` 创建 archive；
- 前端创建或复用一个合成 group id，例如 `agent_user_${userId}`；
- 如有必要，调用 `/v1/bot/groups/{group_id}/join` 让当前 archive 成为 active；
- 发送 `/v1/chat/stream`，其中 `current_dir=""`。

### 有目录存档

适合工程维护。

建议流程：

- 用户输入或选择一个目录路径；
- 第一次对话直接调用 `/v1/chat/stream` 并传 `current_dir`；
- 后端在 `meta.environment` 里返回 `archive_id`、`group_id`、`project_key`、`root_dir`；
- 前端把这个映射保存到本地存档列表。

### bot 文件区

新增功能：用户可以上传文件给 bot，文件进入 bot 的文件区，而不是进入设置目录。

定义：

- bot 文件区是某个存档下的通用附件区。
- 它属于 `archive_id/group_id`，不是 `current_dir`。
- 上传到 bot 文件区的文件可以被 bot 在对话中读取、分析、引用或生成处理结果。
- 上传文件不会自动复制到用户设置的工程目录。
- 如果用户明确要求把文件写入工程目录，也必须走后端工程工具的受控复制/替换流程。

用途：

- 上传需求文档、截图、日志、CSV、Word、PDF、音频等；
- 让 bot 分析文件并回答；
- 让 bot 基于附件生成报告或代码；
- 在空目录模式下也能做文件处理。

UI 表现：

- 每个存档右侧或输入框上方有“bot 文件区”入口；
- 支持拖拽上传；
- 支持粘贴文件；
- 支持查看文件列表；
- 支持删除未使用文件；
- 支持把文件附加到下一条消息；
- 支持标记“本轮重点使用这些文件”。

推荐前端模型：

```ts
type BotFile = {
  id: string;
  archiveId: string;
  groupId: string;
  name: string;
  size: number;
  mime: string;
  status: "uploading" | "ready" | "failed" | "deleted";
  uploadedAt: string;
  url?: string;
  note?: string;
};
```

后端需要补充的文件区接口：

- `POST /v1/chat/files/{archive_id}/{group_id}/upload`
  - 已实现为原始 body 上传，避免引入 `python-multipart` 依赖；
  - 参数通过 query 传：`filename`、`user_id`、`user_name`；
  - 保存到该存档工作区的 `uploaded_files/` 或类似目录；
  - 写入文件索引或 KB placeholder；
  - 返回文件 id、文件名、大小、可下载 URL。

- `GET /v1/chat/files/{archive_id}/{group_id}`
  - 列出 bot 文件区文件；
  - 不混入工程目录文件。

- `DELETE /v1/chat/files/{archive_id}/{group_id}/{file_id}`
  - 删除或软删除文件区文件。

- 可选：`POST /v1/chat/files/{archive_id}/{group_id}/{file_id}/attach`
  - 标记文件为下一轮消息附件；
  - 第一版也可以由前端直接把文件名/文件 id 写入用户消息的结构化前缀。

后端实现注意：

- 文件区路径必须在 workspace 根下，例如：
  - `data/workspaces/<archive_id>/<group_id>/uploaded_files/`
- 不能写入 `current_dir`。
- 不能让前端传任意落盘路径。
- 下载仍走现有文件策略，重点防误发、误打开和路径穿越。
- 大文件需要大小提示和资源预算提示，避免本地磁盘、显存或上下文被意外打满。
- 上传后可以触发后台索引，但不应该阻塞上传响应太久。

前端上传调用方式：

```ts
await fetch(`/v1/chat/files/${archiveId}/${groupId}/upload?filename=${encodeURIComponent(file.name)}&user_id=${encodeURIComponent(userId)}&user_name=${encodeURIComponent(userName)}`, {
  method: "POST",
  headers: { "Content-Type": file.type || "application/octet-stream" },
  body: file,
});
```

### 文件区生命周期

文件区需要区分“文件存在”和“本轮使用”。

状态建议：

- `uploaded`：文件已上传，尚未索引；
- `indexing`：后台正在抽取文本/元数据；
- `ready`：可被 bot 使用；
- `failed`：上传成功但索引失败，仍可作为原始文件被读取；
- `attached`：被附加到某一轮用户消息；
- `archived`：从默认列表隐藏，但仍保留；
- `deleted`：软删除，不再给 bot 看到。

索引策略：

- 小文本、CSV、JSON、代码文件：上传后立即轻量索引；
- Office/PDF/图片/音频：上传后只写 placeholder，等用户引用或后台空闲时再做深度索引；
- 大文件：先只记录文件名、大小、类型、用户备注，避免上传接口长时间卡住；
- 索引结果进入现有 KB/文件索引体系，但 UI 仍显示它来自 bot 文件区。

消息引用方式：

- 前端发送消息时可以携带 `attached_file_ids`。
- 如果后端暂时没有结构化字段，第一版可在用户消息前附加一段机器可读文本：

```text
[BOT_FILE_ATTACHMENTS]
- file_id=... name=...
[/BOT_FILE_ATTACHMENTS]

用户正文...
```

后端更推荐补结构化字段，避免模型误读附件列表为用户正文。

文件区和工程目录的边界：

- 上传文件默认只在 bot 文件区；
- bot 可以读取和分析；
- bot 不能因为文件上传就自动写入 `current_dir`；
- 只有用户明确要求“复制到工程/替换工程文件/作为项目文件使用”时，才走工程工具的受控写回流程；
- 写回前最好在工作流树展示“准备写入工程目录”的确认节点。

### 文件预览

前端应支持常见文件预览：

- 文本/代码：内置只读编辑器；
- Markdown：渲染 + 原文切换；
- CSV/Excel：表格预览，默认只显示前几百行；
- 图片：缩放查看；
- PDF：浏览器 PDF 预览；
- 音频：播放器；
- Word/PPT：第一版显示元信息和下载，后续可接转换预览。

预览不等于 bot 已理解文件内容。UI 应分开显示：

- `已上传`
- `已预览`
- `bot 已索引`
- `本轮已引用`

## 多窗口与并发

前端允许多个窗口/多个对话同时存在。

```ts
type ConversationWindow = {
  id: string;
  userId: string;
  archiveId: string;
  groupId: string;
  currentDir: string;
  projectId: string;
  title: string;
  status: "idle" | "running" | "queued" | "interrupted" | "error";
  activeTraceId?: string;
  messages: ChatMessage[];
  workflowRuns: WorkflowRun[];
  pendingQueue: QueuedInput[];
};
```

并发规则：

- 后端锁粒度是 `(archive_id, group_id, user_id)`。
- 不同用户、不同存档、不同项目可以并行。
- 同一个 `(archive_id, group_id, user_id)` 不能同时跑两个任务。
- 前端遇到同锁冲突时，不应该反复请求，应本地排队。
- 后端返回 `409 user_busy` 时，把输入放入该窗口队列，并显示当前 active trace。

### 前端运行状态机

每个窗口维护一个明确状态机：

```text
idle
  -> sending
  -> running
  -> completing
  -> idle

running
  -> interrupting
  -> interrupted
  -> idle

running
  -> failed
  -> idle
```

队列不应改变当前 run 状态，而是附着在窗口上：

- `pendingQueue.length > 0` 时显示“有 N 条待执行”；
- 当前 run `complete` 后，状态短暂进入 `draining_queue`；
- 队首发送成功后进入新一轮 `running`；
- 用户点击停止时，只停止当前 run，不自动清空队列；
- 用户可选择“停止并清空队列”。

跨窗口协调：

- 使用 `BroadcastChannel` 广播同一浏览器内的 run 状态；
- 如果两个窗口同时对同一 archive 发送，后发送者先本地排队；
- 如果还是撞到后端 `409`，以服务端结果为准。

### 空目录 agent 行为

目录为空时不应退化成低能力聊天框。它仍然是成熟 bot，只是没有项目目录工具：

- 可以聊天；
- 可以分析 bot 文件区文件；
- 可以生成文件到 bot 工作区；
- 可以使用普通工具链；
- 可以使用 helper；
- 不可以读取或写入用户项目目录，因为没有 `current_dir`。

UI 上应把空目录显示为“无项目目录”，而不是“功能不可用”。

## 发送消息

因为要 `POST` SSE，不能直接用浏览器原生 `EventSource`。前端应使用 `fetch` 读取 `ReadableStream`，手动解析 SSE。

请求示例：

```json
{
  "archive_id": "...",
  "group_id": "...",
  "user_id": "...",
  "user_name": "...",
  "message": "...",
  "client_msg_id": "...",
  "current_dir": "",
  "project_id": "...",
  "persona_id": "environment"
}
```

说明：

- 前端 UI 叫 bot；
- 由于当前后端仍使用 `environment.md` 文件名，第一版可以继续传 `persona_id=environment`；
- 如果后端之后支持 `persona_id=bot`，前端再切换；
- 每条消息必须有唯一 `client_msg_id`；
- 收到 `meta.environment` 后更新本地 archive/project 映射；
- 收到 `token` 时实时追加 assistant 文本；
- 收到 `complete` 后标记任务完成，并自动处理队列。

### 消息结构扩展

当前 `ChatRequest.message` 是纯文本。为了支持附件、插入、队列和 UI 恢复，前端内部消息应使用结构化对象，再由 API 层降级为当前后端可接受的文本。

```ts
type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system_status" | "inserted_user";
  text: string;
  createdAt: string;
  traceId?: string;
  attachments?: BotFileRef[];
  sendMode?: "normal" | "insert" | "queue";
  status?: "draft" | "sending" | "streaming" | "done" | "failed";
};
```

降级原则：

- 普通文本直接发送；
- 附件优先使用未来结构化字段；
- 若后端未支持附件字段，则在文本前附加机器可读附件段；
- 插入消息调用 `/v1/chat/interrupt_message`；
- 队列消息暂存在本地，不进入后端。

### 错误处理

常见错误处理：

- `409 user_busy`：自动进入队列；
- `422`：显示请求参数错误，保留草稿；
- SSE 断线：标记 run 为 `connection_lost`，同时用 `/v1/chat/active` 和 `/v1/chat/monitor` 尝试恢复状态；
- 后端 `error` 事件：结束当前 assistant 流，保留错误节点；
- 浏览器刷新：从 IndexedDB 恢复 transcript，再查 active run。

## 中断、插入、队列

### 中断任务

按钮：停止。

调用：

```http
POST /v1/chat/abort
```

参数：

```json
{
  "archive_id": "...",
  "group_id": "...",
  "user_id": "..."
}
```

UI 行为：

- 当前 run 标记为 `interrupted_pending`；
- 后续 SSE 仍继续接收直到连接结束；
- 如果后端返回无活跃任务，不弹错误，只显示“当前没有可中断任务”。

### 插入对话内容

按钮：插入当前任务。

现有接口：

```http
POST /v1/chat/interrupt_message
```

用途：

- 用户在 bot 工作中途追加信息、纠正方向、补充约束；
- 后端应在当前任务中作为 round2.5 干预读取；
- round3 回复后，根据前端策略决定是否自动下一轮。

前端策略：

1. `inject_only`
   - 只注入当前任务；
   - 当前 run 结束后不再自动发送同一条；
   - 默认策略。

2. `inject_then_followup`
   - 先注入当前任务；
   - 当前 run 的 round3/complete 后，把同一条内容作为下一轮普通用户输入自动发送；
   - 适合用户中途说“先停一下，完成后继续做 X”。

3. `queue_if_no_active`
   - 如果当前没有活跃任务，自动改为队列消息或立即发送。

前端展示：

- 插入内容在 transcript 中显示为“插入到当前任务”的用户注记；
- 工作流树中显示 `Round 2.5 用户插入` 节点；
- 如果后端暂时没有 ack 事件，前端先乐观展示，并在接口返回失败时回滚或转队列。

后端建议补充：

- `InterruptMessageRequest` 增加：
  - `target_trace_id`
  - `mode`
  - `display_role`
- SSE 增加：
  - `interrupt_message_received`
  - `round2_5_start`
  - `round2_5_done`

### 加入对话到队列

按钮：加入队列。

第一版由前端本地实现：

- 消息加入 `ConversationWindow.pendingQueue`；
- 不立即请求后端；
- 当前 run 收到 `complete` 后，自动取队首发送；
- 用户可删除、重排队列。

队列规则：

- 队列属于单个对话窗口；
- 不是全局队列；
- 如果发送队首时后端仍返回 `409 user_busy`，保留队首并等待下一次 idle/complete/monitor 信号；
- 队列项也可以携带文件附件引用。

队列 UI：

- 输入区上方显示队列条；
- 每个队列项显示前 80 字、附件数量、发送模式；
- 支持编辑队列项正文；
- 支持增删附件；
- 支持拖拽排序；
- 支持“清空队列”；
- 当前项开始发送时从队列移到 transcript，状态为 `sending`。

自动下一轮策略：

- complete 后延迟 300-800ms 再发队首，给 UI 留出状态更新时间；
- 如果用户在这段时间手动发送新消息，手动消息优先，队列继续等待；
- 如果队首引用的文件已删除，发送前提示用户修复，不静默丢附件。

## 工作流树

右侧面板显示可折叠工作流。

层级：

- Run
  - 主进程
    - Round 1 意图分析
    - Round 2 规划与工具循环
    - Round 2.5 用户插入
    - Round 3 最终回复
    - 维护/记忆整理
  - Helpers
    - helper 任务
      - 工具调用
      - stdout/stderr/progress
      - 输出文件
  - Commands
  - Files

节点模型：

```ts
type WorkflowNode = {
  id: string;
  parentId?: string;
  kind:
    | "run"
    | "main"
    | "round"
    | "helper"
    | "tool"
    | "command"
    | "file"
    | "progress"
    | "error";
  title: string;
  status: "pending" | "running" | "done" | "error" | "interrupted";
  startedAt?: number;
  endedAt?: number;
  summary?: string;
  detail?: unknown;
  children: string[];
  collapsed: boolean;
};
```

SSE 映射：

- `meta`：创建 run 节点；
- `progress`：创建或更新主阶段；
- `token`：追加 assistant 输出；
- `done`：标记逻辑回复完成；
- `complete`：标记 run 完成；
- `error`：标记 run 错误；
- `tool_start/tool_done/tool_error`：创建或更新工具节点；
- `tool_progress/command_output`：追加日志；
- `helper_start/helper_update/helper_done`：创建或更新 helper 节点；
- `workflow_start/round_start/round_done`：如果后端提供，优先使用，不再靠 progress 文本推断。

折叠规则：

- 成功完成的工具默认折叠；
- 活跃节点自动展开；
- 错误节点自动展开；
- 原始日志超过 200 行后折叠，只显示尾部；
- 支持按错误、工具名、helper 名过滤。

## 监控与恢复

前端使用两类流：

1. 当前请求的 `/v1/chat/stream`。
2. 可选全局监控 `/v1/chat/monitor`。

恢复策略：

- 页面刷新后调用 `/v1/chat/active`；
- 对选中 archive/group/user 订阅 `/v1/chat/monitor`；
- 如果发现 active trace，但当前窗口没有 SSE 连接，显示“后台任务仍在运行”；
- 后续建议增加 `GET /v1/chat/runs/{trace_id}` 获取最近 run 快照。

## 本地安全与防误操作

本项目是本地任务，不面向公网在线服务。安全策略重点不是多租户隔离，而是防止本机误操作、路径混乱、资源失控和不可恢复的工程写入。

### 路径边界

- bot 文件区只写入当前后端工作区，不写入 `current_dir`。
- 工程目录读写必须走工程工具流程。
- 前端不要提供“任意复制到任意路径”的通用入口。
- 工程写回尽量使用替换/新增明确文件，而不是把临时工作区整体提升到工程目录。
- UI 需要清楚显示：
  - 当前 bot 文件区路径归属；
  - 当前设置目录；
  - 本次操作是否会写入工程目录。

### 本地确认策略

无需做公网式复杂权限系统，但以下操作应有轻量确认：

- 删除 bot 文件区文件；
- 删除本地账号；
- 删除/归档存档；
- 覆盖工程目录文件；
- 批量写入或移动大量文件；
- 执行明显长耗时命令；
- 使用大文件触发 OCR、TTS、绘图或长上下文分析。

确认不应阻断常规开发体验。可以在设置中提供“信任当前目录，减少确认”的本地开关。

### 资源预算

本地服务可能同时跑 LLM、OCR、TTS、绘图、命令和多个 helper。前端应展示资源相关状态：

- 当前是否有任务运行；
- 当前是否有 GPU/长命令/文件索引任务；
- 上传文件大小；
- 大文件是否已索引；
- 长任务是否可中断；
- 队列长度。

前端不需要强行管理显存锁，但需要把“正在执行重资源任务”显示出来，方便用户手动中断或等待。

### 本地数据隐私

数据都在本机，但仍要避免 UI 混淆：

- 不把一个账号的存档默认展示给另一个账号；
- 不把一个存档的 bot 文件区混到另一个存档；
- 不把工程目录文件和 bot 文件区文件混在同一个列表里；
- 日志/工作流详情默认只展示当前窗口相关内容，全局监控放在高级面板。

## UI 布局

整体是工程工具界面，不做营销页。

左侧栏：

- 账号切换；
- 存档/项目列表；
- 新建存档；
- 当前运行状态；
- bot 文件区入口。

顶部栏：

- 当前账号；
- 当前存档；
- 当前目录路径；
- 连接状态；
- 停止按钮。

中间：

- 对话记录；
- 输入框；
- 发送/插入/加入队列控件；
- 文件附件条。

右侧：

- 工作流树；
- 节点详情；
- 命令输出；
- 文件产物列表。

## 页面信息架构

第一版只需要一个主页面，但内部要按面板划分清楚。

### 左侧栏

宽度建议 280-340px，可折叠。

区域：

1. 账号区
   - 当前账号名；
   - 切换账号；
   - 新建账号。

2. 存档/项目区
   - 搜索；
   - 新建存档；
   - 项目列表；
   - 空目录存档列表；
   - 每项显示：标题、目录摘要、最后活动时间、是否有运行中任务。

3. bot 文件区快捷入口
   - 当前存档文件数量；
   - 最近上传文件；
   - 打开文件区面板。

4. 本地运行状态
   - 当前活跃 run 数；
   - 队列数量；
   - 重资源任务提示。

### 中间对话区

主体是 transcript + composer。

消息类型：

- 用户普通消息；
- 用户插入消息；
- 用户队列消息；
- assistant 流式消息；
- 系统状态消息；
- 文件附件条；
- 错误/中断提示。

消息展示规则：

- 用户消息右侧或上方显示发送模式：发送、插入、队列；
- assistant 消息 streaming 时显示光标/状态；
- 关联文件用小型附件 chip；
- 关联 run 可点击后在右侧工作流树定位；
- 失败消息保留“重试/加入队列/复制”操作。

### 输入区

输入区需要支持三种提交模式：

- `发送`：立即发送，若当前窗口运行中则提示改为队列；
- `插入`：要求当前窗口有活跃 run，调用 interrupt_message；
- `加入队列`：只进入本地队列。

控件：

- 文本输入；
- 附件选择；
- 文件上传按钮；
- 当前 attached files 列表；
- 发送模式分段控件；
- 停止按钮；
- 队列展开按钮。

快捷键建议：

- `Enter` 发送；
- `Shift+Enter` 换行；
- `Ctrl+Enter` 强制发送或按当前模式执行；
- `Esc` 聚焦停止/取消菜单，但不直接中断，避免误触。

### 右侧工作流区

宽度建议 360-520px，可折叠，可独立滚动。

顶部：

- 当前 trace id；
- run 状态；
- 展开全部；
- 只看错误；
- 只看活跃；
- 搜索。

主体：

- 工作流树；
- 选中节点详情；
- 日志 tail；
- 产物列表。

默认行为：

- 当前活跃节点自动滚入视图；
- 用户手动滚动后暂停自动滚动；
- 错误节点自动展开；
- 完成节点自动折叠。

### bot 文件区面板

可以做成右侧 drawer 或中间 modal。

功能：

- 上传；
- 拖拽；
- 文件列表；
- 搜索/过滤；
- 预览；
- 附加到当前输入；
- 删除；
- 显示文件是否已被索引。

文件列表列：

- 文件名；
- 大小；
- 类型；
- 上传时间；
- 状态；
- 操作。

附件交互：

- 文件区中勾选文件后，加入当前输入框附件条；
- 附件条中的文件可以移除；
- 附件 chip 点击打开预览；
- 发送后，附件 chip 固定在对应用户消息下；
- bot 回复中如引用了文件，前端可把文件 chip 高亮；
- 若用户切换存档，附件条必须清空，避免跨存档误引用。

拖拽行为：

- 拖到文件区：只上传，不自动附加；
- 拖到输入框：上传并自动附加到当前输入；
- 拖到 transcript：不触发上传，避免误操作。

### 设置面板

第一版只做本地设置：

- 后端地址；
- 是否自动连接 monitor；
- 是否减少本地确认；
- 日志显示行数；
- 默认发送模式；
- 默认是否自动打开工作流区。

不要做复杂权限、团队、云同步设置。

## 常用功能清单

以下功能按“本地 Codex 类助手”的日常使用频率排序。第一版可以分阶段实现，但 UI 和状态模型应提前预留入口。

### 项目与存档

- 新建空目录存档；
- 新建目录存档；
- 重命名存档；
- 修改存档目录；
- 复制存档；
- 归档存档；
- 删除本地存档记录；
- 打开最近使用；
- 按目录路径搜索存档；
- 按标题搜索存档；
- 显示当前存档的 bot 文件数量、最近 run、队列数量。

目录修改规则：

- 修改目录只影响后续请求；
- 已有 transcript 不重写；
- bot 文件区不随目录移动；
- 如果目录不存在，存档可保留，但发送工程任务前提示修复路径。

### 会话管理

- 新建对话窗口；
- 重命名窗口；
- 关闭窗口；
- 固定窗口；
- 复制窗口；
- 清空当前窗口 transcript；
- 导出当前对话为 Markdown；
- 导出当前 run 工作流日志；
- 跳转到某个 trace；
- 搜索当前对话；
- 按文件名/工具名/错误过滤对话。

会话和存档关系：

- 一个存档可以有多个本地窗口；
- 窗口只是前端视图，不强行创建新 archive；
- 同一存档多个窗口共享后端锁；
- 同一存档多个窗口可以有不同本地队列，但发送时仍按锁串行。

### 输入与编辑

- 多行输入；
- 粘贴图片/文件；
- 拖拽文件；
- 从 bot 文件区选择附件；
- 清空输入；
- 保存草稿；
- 历史输入上下翻；
- 输入模板；
- 常用提示词片段；
- 发送前预览附件；
- 发送前选择模式：发送、插入、队列。

草稿规则：

- 每个窗口独立草稿；
- 草稿自动保存；
- 切换存档不丢草稿；
- 附件引用随草稿保存，但如果附件被删，显示失效。

### 文件与产物

- 上传文件到 bot 文件区；
- 文件列表搜索；
- 文件预览；
- 附加文件到消息；
- 下载 bot 文件；
- 删除 bot 文件；
- 查看 bot 生成产物；
- 下载生成产物；
- 复制文件路径或 URL；
- 标记文件为“本轮重点”；
- 对文件发起快捷操作：
  - 总结；
  - 提取文本；
  - 转表格；
  - 生成报告；
  - 对比两个文件；
  - 解释错误日志。

文件快捷操作不应绕过对话流程。点击快捷操作本质是向输入框填入一条可编辑的请求，并附加对应文件。

### 工程目录浏览

第一版可以不做完整文件树，但应预留。

常用功能：

- 显示当前目录路径；
- 检查目录是否存在；
- 打开目录最近状态；
- 请求 bot 列出目录；
- 请求 bot 搜索文件；
- 请求 bot 分析项目结构；
- 请求 bot 查看某个文件；
- 请求 bot 修改某个文件；
- 请求 bot 运行测试。

注意：

- 前端不能直接编辑工程文件；
- 前端可以展示 bot 工具返回的目录/文件摘要；
- 真正修改仍走后端受控工作流。

### 命令与运行

- 显示当前活跃命令；
- 显示命令 stdout/stderr；
- 中断命令；
- 复制命令；
- 复制输出；
- 折叠长输出；
- 标记错误行；
- 从命令失败快速生成“请修复这个错误”的下一轮请求。

命令输出处理：

- 默认显示 tail；
- 完整输出可保存到工作流日志；
- 超长输出不要塞入 transcript；
- 错误摘要可以显示在 assistant 回复旁边。

### Diff 与变更查看

工程 agent 前端最终需要能看变更。

常用功能：

- 显示 bot 准备写回的文件；
- 显示文件 diff；
- 显示新增/删除/修改统计；
- 折叠大 diff；
- 复制 diff；
- 请求 bot 解释 diff；
- 请求 bot 继续修改；
- 请求 bot 回滚本轮工作区修改。

第一版可先只展示后端事件/文本中的文件名；真正 diff 需要后端提供环境工具结果或新增 diff endpoint。

### 工作流控制

- 停止当前任务；
- 插入当前任务；
- 加入队列；
- 暂停自动跑队列；
- 清空队列；
- 重试失败任务；
- 从某条消息重新发送；
- 把某条 assistant 回复作为上下文继续追问；
- 把某个文件/错误节点作为上下文继续追问。

重试规则：

- 重试应生成新的 `client_msg_id`；
- 默认复用原消息文本和附件；
- 如果原消息是插入消息，重试时转成普通发送，除非用户仍选择插入。

### 搜索

搜索范围：

- 当前 transcript；
- 当前工作流；
- 当前 bot 文件区；
- 当前存档本地窗口；
- 所有本地存档标题；
- 未来可接后端 KB 搜索。

搜索结果：

- 按类型分组；
- 点击定位；
- 工作流日志命中时展开相关节点；
- 文件命中时打开文件区预览。

### 通知与状态

本地前端应有轻量通知：

- 任务完成；
- 任务失败；
- 需要用户确认；
- 文件上传完成；
- 文件上传失败；
- 队列开始执行；
- 后端连接断开；
- 后端恢复。

通知不应干扰工作流：

- 默认显示在右下角或状态栏；
- 不用浏览器系统通知作为第一版；
- 错误通知可点击定位到节点。

### 快捷键

建议快捷键：

- `Ctrl+K`：打开快速命令；
- `Ctrl+L`：聚焦输入框；
- `Ctrl+Shift+F`：搜索当前窗口；
- `Ctrl+Shift+O`：切换存档；
- `Ctrl+Enter`：按当前模式提交；
- `Shift+Enter`：换行；
- `Ctrl+.`：打开/关闭工作流面板；
- `Ctrl+B`：打开/关闭左侧栏；
- `Esc`：关闭浮层。

停止任务不要默认绑定单键，避免误触。可用 `Ctrl+Backspace` 或通过按钮/命令面板触发。

### 命令面板

实现类似编辑器的命令面板：

- 新建存档；
- 切换账号；
- 切换存档；
- 上传文件；
- 打开 bot 文件区；
- 停止当前任务；
- 清空队列；
- 搜索当前对话；
- 打开设置；
- 复制 trace id；
- 导出当前对话。

命令面板可以显著减少 UI 按钮堆积。

### 设置与偏好

本地设置：

- 后端地址；
- 默认账号；
- 默认发送模式；
- 默认是否打开工作流面板；
- 默认是否连接 monitor；
- 日志 tail 行数；
- 大文件提示阈值；
- 减少确认开关；
- 主题：浅色/深色/跟随系统；
- 字体大小；
- 代码字体；
- 自动保存间隔。

存档级设置：

- 标题；
- 目录路径；
- 默认是否自动附加最近上传文件；
- 默认工作流折叠级别；
- 是否启用队列自动执行。

窗口级设置：

- 当前草稿；
- 当前附件；
- 当前发送模式；
- 工作流筛选条件。

### 可访问性与可读性

- 所有按钮有明确 title；
- 图标按钮有 tooltip；
- 颜色不作为唯一状态表达；
- 错误/运行/完成状态有文字和图标；
- 长文本不溢出；
- 小屏幕下右侧工作流改为 drawer；
- 字号可调。

## 常用任务模板

模板不是硬编码工作流，只是帮用户快速填充输入框。用户发送前可以编辑。

### 工程类模板

- “阅读当前项目结构，指出主要模块和启动方式。”
- “运行项目测试并修复失败。”
- “检查最近日志，找出异常和优化点。”
- “实现这个功能：...”
- “重构这部分代码，保持行为等价，并运行测试。”
- “为这个模块补测试。”
- “解释这个错误并修复：...”
- “检查是否有死代码或重复逻辑。”
- “生成一份变更摘要。”

### 文件类模板

- “总结这个文件。”
- “提取这个文件里的关键数据。”
- “把这个文件整理成表格。”
- “根据这些文件生成报告。”
- “比较这两个文件的差异。”
- “检查这份文档是否有不实数据或图表错误。”
- “把这个日志里的错误按严重程度分类。”

### 对话类模板

- “继续刚才的任务。”
- “更详细解释上一轮结果。”
- “只给我结论。”
- “把结果整理成 Markdown。”
- “把结果整理成待办清单。”
- “忽略上一轮的方向，改成：...”

### 模板实现方式

模板数据本地保存：

```ts
type PromptTemplate = {
  id: string;
  title: string;
  category: "project" | "file" | "chat" | "custom";
  body: string;
  createdAt: string;
  updatedAt: string;
};
```

模板只填充输入框，不直接发送，除非用户在设置里打开“模板点击即发送”。

## 快捷操作转对话

为了避免前端硬编码大量工具逻辑，所有快捷操作都应转成一条普通用户请求。

例子：

- 文件“总结”：
  - 输入框填入：“总结这个文件，列出关键点和可能需要注意的问题。”
  - 自动附加该文件 id。

- 命令失败“修复”：
  - 输入框填入：“刚才命令失败了，请根据错误输出定位原因并修复。”
  - 自动关联对应 workflow node。

- Diff“解释”：
  - 输入框填入：“解释这组改动的目的、风险和需要验证的地方。”
  - 自动关联 diff node。

这样前端负责交互便利性，决策仍交给 bot 和后端工具链。

## 技术栈建议

推荐：

- Vite + React + TypeScript；
- Zustand 或 Redux Toolkit；
- IndexedDB/Dexie 保存本地账号、存档、窗口、文件元信息；
- 普通 CSS 或 CSS Modules。

原因：

- 本地开发快；
- POST SSE 解析容易控制；
- 工作流树和多窗口状态适合前端 store；
- 后续迁移 Electron/Tauri 也方便。

## 本地持久化设计

使用 IndexedDB 保存本地 UI 状态。不要把所有内容都放 localStorage；localStorage 只保存当前账号 id、后端地址这类小配置。

建议 Dexie 表：

```ts
type DbSchema = {
  accounts: Account;
  archives: AgentArchive;
  conversations: ConversationWindowSnapshot;
  messages: ChatMessage;
  workflowRuns: WorkflowRunSnapshot;
  workflowNodes: WorkflowNode;
  botFiles: BotFile;
  settings: LocalSetting;
};
```

保存策略：

- 账号、存档、文件元信息：立即保存；
- transcript：每条消息状态变化后保存；
- assistant token：节流保存，例如 500ms 或 2KB 一次；
- workflow node：节流保存，错误/完成立即保存；
- 原始日志：只保存 tail，长日志写入单独 blob 或只保存在内存。

清理策略：

- 每个窗口默认保留最近 50 个 run；
- 每个 run 默认保留结构化节点，原始日志只保留尾部；
- bot 文件区文件删除后，本地索引同步删除；
- 后端已删除但本地仍存在的文件，列表刷新时标记为 `missing`。

恢复策略：

1. 页面启动读取 settings 和当前账号。
2. 读取账号下 archive/conversation。
3. 调用 `/v1/chat/active`。
4. 对活跃 archive/group/user 订阅 `/v1/chat/monitor`。
5. 将 monitor snapshot 合并到对应窗口。

冲突处理：

- 本地状态和后端 active 冲突时，后端 active 优先；
- 本地认为 running 但后端无 active：标记为 `unknown_completed`，提示用户查看日志或继续；
- 后端有 active 但本地无窗口：创建一个“恢复的后台任务”窗口。

## API 客户端设计

前端 API 层分四块：

1. `chat.ts`
   - `streamChat(request, handlers)`
   - `abortChat(ids)`
   - `insertMessage(request)`

2. `files.ts`
   - `uploadBotFile(file, ids)`
   - `listBotFiles(ids)`
   - `deleteBotFile(ids)`

3. `archives.ts`
   - `createArchive`
   - `listArchives`
   - 后续接 `/v1/agent/projects`

4. `monitor.ts`
   - `subscribeMonitor(filter, handlers)`
   - `getActive()`
   - `abortCommand(commandId)`

### POST SSE 解析

实现一个通用解析器：

```ts
type SseMessage = {
  event: string;
  data: unknown;
  id?: string;
  retry?: number;
};
```

解析规则：

- 按空行分隔事件；
- 支持多行 `data:`；
- `event:` 缺省为 `message`；
- JSON parse 失败时保留 raw string 并生成解析错误节点；
- 网络断开时抛出 `SseConnectionError`，由调用方决定恢复。

取消：

- 每个 stream 使用 `AbortController`；
- 用户停止任务时先调用后端 `/abort`，不要只 abort fetch；
- 用户关闭窗口时可以只 abort fetch，但要提示后台任务可能仍在运行。

### API 错误模型

统一错误对象：

```ts
type ApiError = {
  code: string;
  message: string;
  status?: number;
  detail?: unknown;
  recoverable: boolean;
};
```

常见 code：

- `user_busy`
- `archive_mismatch`
- `archive_not_found`
- `upload_too_large`
- `connection_lost`
- `sse_parse_error`
- `backend_error`

## 目录结构规划

```text
agent_frontend/
  package.json
  index.html
  src/
    app/
      App.tsx
      routes.ts
    api/
      client.ts
      sse.ts
      chat.ts
      archives.ts
      files.ts
      monitor.ts
    state/
      accounts.ts
      archives.ts
      conversations.ts
      files.ts
      workflow.ts
    components/
      AccountSwitcher.tsx
      ArchiveSidebar.tsx
      BotFilePanel.tsx
      ChatTranscript.tsx
      Composer.tsx
      DirectoryPicker.tsx
      FileAttachmentBar.tsx
      WorkflowTree.tsx
      NodeDetails.tsx
    types/
      api.ts
      domain.ts
    styles/
      tokens.css
      app.css
```

## 后端缺口

前端可先基于现有接口启动，但以下能力最好补齐：

1. bot 身份
   - 已经直接改 `personas/environment.md` 内容为 bot。
   - 后续可以继续保留 `persona_id=environment` 兼容。

2. 用户项目/存档管理
   - `GET /v1/agent/projects?user_id=...`
   - `POST /v1/agent/projects`
   - `PATCH /v1/agent/projects/{project_id}`
   - 用来包装现有 `environment_projects.json`。

3. bot 文件区
   - 上传：已实现 `/v1/chat/files/{archive_id}/{group_id}/upload`。
   - 列表：已实现 `GET /v1/chat/files/{archive_id}/{group_id}`。
   - 删除：已实现 `DELETE /v1/chat/files/{archive_id}/{group_id}/{file_id}`。
   - 附加到消息：已实现 `ChatRequest.attached_file_ids`，后端会注入附件说明。
   - 后台索引：当前写入现有 file KB 节点，后续可由 dream 做深度索引。
   - 本地防误操作：路径归属明确、删除确认、大文件提示。

4. 插入消息语义
   - 指定 trace；
   - 指定模式；
   - SSE ack；
   - round2.5 生命周期事件。

5. Run 快照
   - 最近 run 历史；
   - active trace 详情；
   - 页面刷新后恢复。

6. 事件标准化
   - `round_start/round_done`；
   - helper 生命周期；
   - 工具 parent id；
   - command 输出分块。

7. 目录选择
   - 纯浏览器第一版只能输入/粘贴路径；
   - 真正系统目录选择需要桌面壳或后端本地 picker。

8. 本地运行状态
   - 暴露当前活跃任务；
   - 暴露重资源任务；
   - 暴露队列长度；
   - 支持按 trace 查看最近事件。

## 建议后端接口草案

### 项目/存档接口

```http
GET /v1/agent/projects?user_id=...
POST /v1/agent/projects
PATCH /v1/agent/projects/{project_id}
```

`POST /v1/agent/projects` 请求：

```json
{
  "user_id": "local-user",
  "title": "chatbot",
  "current_dir": "F:/chatbot",
  "archive_id": "",
  "persona_id": "environment"
}
```

返回：

```json
{
  "project_id": "...",
  "archive_id": "...",
  "group_id": "...",
  "user_id": "...",
  "title": "chatbot",
  "current_dir": "F:/chatbot",
  "created_at": "...",
  "last_seen_at": "..."
}
```

说明：

- `current_dir` 可以为空；
- 空目录项目也应有 archive/group；
- 这只是包装现有 archive 与 environment project mapping，不改 orchestrator。

### bot 文件区接口

```http
POST /v1/chat/files/{archive_id}/{group_id}/upload?filename=...&user_id=...&user_name=...
GET /v1/chat/files/{archive_id}/{group_id}
DELETE /v1/chat/files/{archive_id}/{group_id}/{file_id}
```

上传请求：

- 原始 request body；
- `Content-Type` 使用文件 MIME；
- `filename/user_id/user_name` 通过 query 传；
- 不需要 `python-multipart`。

上传返回：

```json
{
  "ok": true,
  "file": {
    "id": "...",
    "archive_id": "...",
    "group_id": "...",
    "name": "requirements.pdf",
    "size": 123456,
    "mime": "application/pdf",
    "status": "uploaded",
    "uploaded_at": "...",
    "download_url": "/v1/chat/files/.../uploaded_files/requirements.pdf"
  }
}
```

### 结构化聊天请求扩展

现有 `ChatRequest` 可兼容扩展：

```json
{
  "archive_id": "...",
  "group_id": "...",
  "user_id": "...",
  "message": "请分析这些文件",
  "current_dir": "",
  "project_id": "...",
  "attached_file_ids": ["..."],
  "client_msg_id": "..."
}
```

如果暂时不改 schema，前端先用文本附件段降级。

### 标准事件建议

```json
{
  "event": "tool_start",
  "trace_id": "...",
  "node_id": "...",
  "parent_id": "...",
  "title": "读取文件",
  "kind": "tool",
  "status": "running",
  "ts": 1234567890
}
```

所有新增事件建议包含：

- `trace_id`
- `node_id`
- `parent_id`
- `kind`
- `status`
- `title`
- `summary`
- `ts`

这样前端可以稳定构建树，不靠自然语言解析。

## 实施阶段

### 阶段 1：静态壳与 API 客户端

- 建 Vite React 工程；
- 配置后端 base URL；
- 实现 POST SSE 解析；
- 实现账号切换；
- 实现基础聊天面板。

验收：

- 可以创建/选择账号；
- 可以向已有 archive/group 发消息；
- 可以传 `current_dir` 进入工程模式；
- token 实时显示。

细分任务：

1. 初始化 Vite React TS。
2. 写 `api/sse.ts`，用假 ReadableStream 单测解析。
3. 写 `api/chat.ts`，先只支持 `streamChat`。
4. 写最小 App：左侧账号 + 中间 transcript + 输入框。
5. 接真实 `/v1/chat/stream`。
6. 保存 transcript 到 IndexedDB。

不在本阶段做：

- 文件上传；
- 工作流树；
- 多窗口；
- 插入/队列。

### 阶段 2：存档/目录/bot 文件区

- 新建存档弹窗；
- 目录路径输入；
- 本地保存 archive/project；
- 从 `meta.environment` 更新本地映射；
- 文件区面板；
- 文件上传 UI；
- 文件附加到消息。
- 文件删除确认；
- 大文件提示；
- 文件区和工程目录的视觉隔离。

验收：

- 同一用户多个存档可切换；
- 空目录模式正常聊天；
- 有目录模式正常进入工程；
- 上传文件不会进入设置目录；
- 消息可引用 bot 文件区文件。
- 用户能明确看出“这个文件在 bot 文件区，不在项目目录”。

细分任务：

1. 存档列表本地模型。
2. 新建空目录存档。
3. 新建目录存档。
4. bot 文件区 API client。
5. 文件区 drawer。
6. 上传进度。
7. 文件附加到输入框。
8. 发送消息带 `attached_file_ids`。

重点检查：

- 上传接口是 raw body，不是 multipart。
- 删除文件只删除 bot 文件区，不碰工程目录。
- 文件列表刷新以后本地状态和后端一致。

### 阶段 3：工作流树

- SSE 事件标准化；
- 右侧可折叠工作流树；
- helper/tool/command 节点；
- 错误与日志详情。

验收：

- 主流程阶段可见；
- helper/tool 事件出现时能归入当前 run；
- 错误节点展开；
- 长日志可折叠。

细分任务：

1. 定义 `WorkflowRun` / `WorkflowNode` store。
2. 把现有 `meta/progress/token/done/complete/error` 映射成最小树。
3. 支持后端额外 tool/helper 事件。
4. 右侧树 UI。
5. 节点详情面板。
6. 日志 tail 组件。
7. 搜索和过滤。

兼容策略：

- 后端事件不完整时，前端用 progress 推断主阶段；
- 后端提供 parent id 时，优先使用 parent id；
- 推断结果标记 `inferred=true`，便于调试。

### 阶段 4：中断、插入、队列

- 停止按钮；
- 插入当前任务；
- 本地队列；
- complete 后自动下一轮。

验收：

- 长任务可中断；
- 活跃任务可插入修正；
- 队列消息按顺序自动执行；
- 409 不会造成重复请求风暴。

细分任务：

1. Stop 按钮。
2. 本地队列 store。
3. complete 后 drain 队列。
4. 409 自动入队。
5. insert mode UI。
6. `/interrupt_message` 调用。
7. `inject_only` / `inject_then_followup` 策略。
8. 队列项编辑、删除、重排。

关键语义：

- 插入不是普通下一轮，除非用户选择 `inject_then_followup`。
- 停止当前任务不清空队列。
- 同一锁目标只允许一个正在发送的请求。

### 阶段 5：多窗口与恢复

- 会话 tabs；
- BroadcastChannel 同步多浏览器窗口；
- `/v1/chat/active` 和 `/v1/chat/monitor` 恢复活跃任务。

验收：

- 不同项目可并行；
- 同一锁目标自动排队；
- 刷新页面后仍知道后台任务是否在跑。

细分任务：

1. 会话 tab。
2. 每个 tab 独立 transcript/workflow/queue。
3. BroadcastChannel 同步运行状态。
4. 页面启动恢复 active。
5. monitor 订阅。
6. 后台任务窗口重建。
7. 多窗口冲突测试。

完成标准：

- 两个不同目录可以同时跑。
- 同一目录两个窗口不会互相打爆后端。
- 刷新页面后至少能看到“任务还在跑/没有任务在跑”的真实状态。

## 需要继续确认的点

- 空目录 agent chat 是否立即新增 `/v1/agent/projects`，还是第一版用 `/v1/archives` + 合成 group id。
- 文件上传后是否立即触发 KB 索引，还是等用户在消息中引用时再索引。
- 插入内容默认是否显示在普通 transcript 中，还是只显示在工作流树中。
- 原始工具日志在本地保留多久。

## 测试清单

### 基础聊天

- 创建账号；
- 创建空目录存档；
- 发送普通消息；
- 收到流式 token；
- complete 后状态回到 idle；
- 刷新页面后本地 transcript 存在。

### 工程目录

- 输入有效目录；
- 首次发送后从 `meta.environment` 保存 archive/group/project；
- 切换到另一个目录；
- 切回旧目录；
- 空目录和有目录存档互不混淆。

### bot 文件区

- 上传 txt；
- 上传图片；
- 上传 PDF；
- 上传大文件时出现提示；
- 删除文件需要确认；
- 文件列表不显示工程目录文件；
- 设置目录不出现上传文件；
- 消息引用文件后 bot 能看到文件信息；
- 上传失败不丢失用户草稿。

### 队列

- 当前 run 中加入队列；
- complete 后自动发送队首；
- 多条队列按顺序执行；
- 409 后不重复风暴；
- 停止当前任务不自动清空队列；
- 用户可手动清空队列。

### 插入

- 活跃 run 中插入文本；
- UI 显示插入注记；
- `inject_only` 不自动下一轮；
- `inject_then_followup` 在 complete 后自动下一轮；
- 无活跃 run 时转为普通发送或队列。

### 中断

- 长任务中断；
- 无任务中断返回友好状态；
- 中断后队列仍保留；
- 中断后可继续发送新消息。

### 工作流树

- progress 显示主阶段；
- tool/helper 事件能折叠；
- 错误自动展开；
- 长日志折叠；
- 刷新后能通过 monitor 看到活跃任务。

### 本地防误操作

- bot 文件区上传不写工程目录；
- 工程写回有清晰提示；
- 删除账号/存档/文件有确认；
- 多账号文件列表隔离；
- 多存档文件列表隔离。

## 初始建议

先做 Vite React TypeScript 前端壳，直接接 `/v1/chat/stream`。同时优先补后端 bot 文件区上传接口，因为这是当前现有 API 最缺的一块；其它如 run 快照、round2.5 ack、事件 parent id 可以在工作流树实际接入时逐步补。
