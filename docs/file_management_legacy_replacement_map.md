# 文件系统旧链路替换映射

## 原则

旧模块只保留外部接口兼容，不再作为文件事实来源。所有新功能应优先接入 `app/core/filesystem`。

## 已建立的新核心

- `app/core/filesystem/models.py`：统一文件记录模型。
- `app/core/filesystem/path_resolver.py`：统一 project/workspace/staged 路径解析。
- `app/core/filesystem/registry.py`：JSON-backed registry。
- `app/core/filesystem/indexer.py`：项目索引和 manifest summary。
- `app/core/filesystem/planner.py`：read plan 分片。
- `app/core/filesystem/transfers.py`：stage/intake/promote 文件流。
- `app/core/filesystem/artifacts.py`：registry-backed deliverable view。

## 已替换

### `environment_resources.build_project_resource_manifest`

旧职责：

- 自行 os.walk 项目目录。
- 自行统计分类、suffix、key path。
- 自行写旧 resource manifest。

新职责：

- 调用 `index_project` 建立 `.file_registry.json`。
- 从 registry 派生旧 `.resource_manifest.json` 兼容视图。
- 旧 manifest 增加 `source=file_registry`、`registry_path`、`file_id`、`sha256`。

## 下一批替换入口

### 1. `_auto_fetch_environment_workspace_refs`

文件：`app/llm/tools/delegate_actions.py`

旧问题：

- 手写 regex 提取路径。
- 手写 context refs。
- 手写目录展开和 `_env` fetch。
- 与 `environment_resources.explicit_project_refs` 重复。

替换方向：

- 用 `index_project` 建立 registry。
- 用 planner 从 task fields/prompt 生成 input plan。
- 用 `stage_project_file` 复制项目文件。
- 返回 fetched/skipped 兼容结构。

### 2. `environment._handle_fetch`

文件：`app/llm/tools/environment.py`

旧问题：

- 写 `_env/.manifest.json`。
- 自己记录 sha256 和 workspace_path。

替换方向：

- 调用 `stage_project_file`。
- 旧 `.manifest.json` 由 registry 派生兼容写入。

### 3. `delegate_copyback._copy_results_to_main`

文件：`app/llm/tools/delegate_copyback.py`

旧问题：

- 扫描 helper workspace。
- 依赖 expected_outputs、declared_files、文件名前缀和 cap。
- 同时承担污染控制、产物命名、shared merge、project staged merge。

替换方向：

- output intake：所有 helper 新文件先注册为 `helper_output` 或 `evidence`。
- verification：根据 task contract 标记 verified/failed。
- promotion：只有 verified 文件能转为 `project_edit` 或 `deliverable`。
- cap 只作为异常安全阀，不参与正常文件识别。

### 4. `environment._env_apply_provenance_guard`

文件：`app/llm/tools/environment.py`

旧问题：

- 依赖 `_env/.provenance.json` 判断 ready。

替换方向：

- 查 registry 中对应 staged/project record 的 `status/verified/apply_state`。
- provenance 只作为旧兼容视图。

### 5. `workspace.list_generated_files`

文件：`app/llm/tools/workspace.py`

旧问题：

- 扫描工作区推断 generated/artifact。

替换方向：

- 只读 `list_deliverable_records`。
- 内部文件、evidence、scratch 不进入前端 artifact。

### 6. `memory.bot_artifacts`

文件：`app/memory/bot_artifacts.py`

旧问题：

- 记录 done.files，但缺少 registry file_id/source 状态。

替换方向：

- done.files 进入 `promote_deliverable`。
- DB 表保留为前端查询缓存，内容来自 registry record。

## 需要删除或降级的旧状态文件

- `_env/.manifest.json`
- `_env/.resource_manifest.json`
- `_env/.provenance.json`
- display name remap 文件
- generated file scan cache

迁移期这些文件可以继续生成，但必须标记为 compatibility view。

## 后续测试要求

每替换一个入口至少补充：

- 中文路径测试。
- 空格/括号路径测试。
- 大量文件目录测试。
- helper 输出污染测试。
- artifact 只显示 deliverable 测试。
- read coverage 未完成时的 retry plan 测试。
