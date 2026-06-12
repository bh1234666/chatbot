"""Shared framework contract and helper-request formatting utilities."""
from __future__ import annotations

import os
import re
from typing import Any


def normalize_framework_contract(raw: Any, *, max_chars: int = 1800) -> str:
    """Return a compact helper-visible framework contract string."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        parts: list[str] = []
        for key in (
            "goal",
            "purpose",
            "role",
            "from_file",
            "contract",
            "framework",
            "interfaces",
            "schema",
            "outline",
            "template",
            "required_subsections",
            "evidence_map",
            "validation",
            "merge_order",
            "ownership",
            "output",
            "expected_outputs",
            "acceptance",
            "acceptance_checks",
        ):
            value = raw.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (list, tuple)):
                value_text = "; ".join(
                    normalize_framework_contract(x, max_chars=max_chars)
                    for x in value
                    if str(x).strip()
                )
            elif isinstance(value, dict):
                value_text = "; ".join(
                    f"{k}: {normalize_framework_contract(v, max_chars=max_chars)}"
                    for k, v in sorted(value.items(), key=lambda item: str(item[0]))
                    if v not in (None, "", [], {})
                )
            else:
                value_text = str(value)
            if value_text.strip():
                parts.append(f"{key}: {value_text.strip()}")
        if not parts:
            parts = [
                f"{k}: {normalize_framework_contract(v, max_chars=max_chars)}"
                for k, v in sorted(raw.items(), key=lambda item: str(item[0]))
                if v not in (None, "", [], {})
            ]
        text = "\n".join(parts)
    elif isinstance(raw, (list, tuple)):
        text = "\n".join(str(x).strip() for x in raw if str(x).strip())
    else:
        text = str(raw)
    text = re.sub(r"\s+\n", "\n", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[framework truncated by system]"
    return text


def normalize_string_list(raw: Any, *, max_items: int = 30, max_chars_each: int = 260) -> list[str]:
    """Normalize a task metadata field into a compact string list."""
    if raw in (None, "", [], {}):
        return []
    if isinstance(raw, str):
        values = [line.strip(" -\t") for line in raw.splitlines()]
    elif isinstance(raw, dict):
        values = [
            f"{k}: {raw[k]}"
            for k in sorted(raw.keys(), key=lambda item: str(item))
        ]
    elif isinstance(raw, set):
        values = sorted((str(x).strip() for x in raw), key=str)
    elif isinstance(raw, (list, tuple)):
        values = [str(x).strip() for x in raw]
    else:
        values = [str(raw).strip()]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        if len(value) > max_chars_each:
            value = value[:max_chars_each].rstrip() + "..."
        result.append(value)
        if len(result) >= max_items:
            break
    return result


def _bullet_list(values: list[str], *, empty: str) -> str:
    if not values:
        return f"- {empty}"
    return "\n".join(f"- {value}" for value in values)


def format_helper_request_envelope(task: dict) -> str:
    """Return the model-visible canonical helper request envelope."""
    prompt = str(task.get("prompt") or "").strip()
    contract = normalize_framework_contract(task.get("framework"), max_chars=2400)
    input_files = normalize_string_list(
        task.get("input_files")
        or task.get("source_files")
        or task.get("transferred_files")
        or task.get("files")
    )
    expected_outputs = normalize_string_list(task.get("expected_outputs"), max_items=40)
    write_scopes = normalize_string_list(task.get("write_scopes"), max_items=30)
    acceptance_checks = normalize_string_list(task.get("acceptance_checks") or task.get("checks"))
    dispatch_reason = str(
        task.get("dispatch_reason")
        or task.get("routing_reason")
        or task.get("delegation_reason")
        or ""
    ).strip()
    if len(dispatch_reason) > 1200:
        dispatch_reason = dispatch_reason[:1200].rstrip() + "..."
    kind = str(task.get("kind") or "code").strip() or "code"
    mode = str(task.get("mode") or "easy").strip() or "easy"
    task_id = str(task.get("task_id") or "").strip() or "unnamed"
    resume = "true" if task.get("resume") else "false"
    fork_from = str(task.get("fork_from") or "").strip()

    return (
        "## Helper Request Envelope\n"
        f"- task_id: {task_id}\n"
        f"- helper_kind: {kind}\n"
        f"- helper_mode: {mode}\n"
        f"- resume: {resume}\n"
        f"- fork_from: {fork_from or 'none'}\n\n"
        "### Shared Framework Contract\n"
        f"{contract or 'Not provided. If this task depends on a shared interface, schema, outline, evidence map, validation plan, or merge order, report that the framework is missing instead of inventing one.'}\n\n"
        "### Main Dispatch Reason\n"
        f"{dispatch_reason or 'Not provided. Follow the explicit task envelope and report any boundary, kind, or split concern as a fact.'}\n\n"
        "### Transferred Or Readable Files\n"
        f"{_bullet_list(input_files, empty='Not specified. Inspect only concrete paths provided in the request or existing workspace evidence.')}\n\n"
        "### Expected Outputs\n"
        f"{_bullet_list(expected_outputs, empty='No concrete output files declared. Return a precise report and note any files that should have been declared.')}\n\n"
        "### Writable Project Scopes\n"
        f"{_bullet_list(write_scopes, empty='Same as expected outputs. Ask the main process before editing other staged project paths.')}\n\n"
        "### Project Visibility Fact\n"
        "In environment project work, project validators and check scripts read the real project/workspace state. "
        "Bare output filenames are chat-workspace artifacts. Outputs that must become project-visible should be declared and produced as `_env/<project-relative-path>` staged files for main acceptance/apply, or reported as needing env_apply_* by the main process.\n\n"
        "项目验收可见产物需要 `_env/<项目相对路径>` 暂存输出或主进程 env_apply_*；裸文件名只是聊天工作区产物。\n\n"
        "### Acceptance Checks\n"
        f"{_bullet_list(acceptance_checks, empty='Infer focused local checks from the request, then state exactly what was verified.')}\n\n"
        "### Request Content\n"
        f"{prompt}\n\n"
        "Use this envelope as the task contract. Keep work inside the declared helper kind and mode; ask the main process for missing resources instead of expanding the scope.\n\n"
        "以上请求信封是任务契约；按 helper 类型、模式、框架、文件、产物和验收执行，缺资源时请求主进程。"
    )


def helper_prompt_with_framework(prompt: str, framework: Any) -> str:
    """Inject the shared framework contract into a helper prompt when present."""
    contract = normalize_framework_contract(framework)
    if not contract:
        return prompt
    return (
        "## Shared Framework Contract\n"
        f"{contract}\n\n"
        "Follow this contract as the source of truth for interfaces, schemas, outline, evidence coverage, validation, ownership, and merge order. "
        "If your assigned slice conflicts with it, report the conflict instead of inventing a new framework.\n\n"
        "共享框架契约用于统一接口、结构、证据、验收和合并顺序；分片任务如有冲突应报告给主进程。\n\n"
        "## Assigned Helper Task\n"
        f"{prompt}"
    )


def _task_text(task: dict) -> str:
    outputs = " ".join(str(x) for x in (task.get("expected_outputs") or []))
    return f"{task.get('task_id','')} {task.get('kind','')} {outputs} {task.get('prompt','')}"


def task_has_framework(task: dict) -> bool:
    """Return True only when a task explicitly carries a framework field."""
    return bool(normalize_framework_contract(task.get("framework")))


def is_compact_framework_contract_task(task: dict, *, max_prompt_chars: int = 2800) -> bool:
    """Return True for a structural framework/spec helper that should run first."""
    if not isinstance(task, dict):
        return False
    kind = str(task.get("kind") or "").strip().lower()
    if kind not in {"code", "edit"}:
        return False
    if task.get("resume"):
        return False
    mode = str(task.get("mode") or "").strip().lower()
    if mode == "hard":
        return False

    outputs = [
        str(output).replace("\\", "/").lstrip("./").strip()
        for output in (task.get("expected_outputs") or [])
        if str(output).strip()
    ]
    if not outputs or len(outputs) > 5:
        return False

    prompt_l = str(task.get("prompt") or "").lower()
    if len(prompt_l) > max_prompt_chars:
        return False

    lowered_outputs = [output.lower() for output in outputs]
    validation_scripts = [
        output
        for output in lowered_outputs
        if output.endswith((".py", ".js", ".ts", ".ps1", ".bat", ".cmd", ".sh"))
    ]
    if len(validation_scripts) > 1:
        return False
    for output in validation_scripts:
        basename = os.path.basename(output)
        if not (
            re.search(r"(?:check|validate|verify|lint|smoke)", basename)
            and re.search(r"(?:framework|contract|spec|schema|outline|manifest|project)", output)
        ):
            return False
    for output in lowered_outputs:
        if output in validation_scripts:
            continue
        if not output.endswith((".md", ".txt", ".json")):
            return False

    task_id = str(task.get("task_id") or "").lower()
    outputs_text = " ".join(lowered_outputs)
    combined = f"{task_id} {outputs_text} {prompt_l}"
    has_contract_role = any(
        marker in combined
        for marker in (
            "framework", "contract", "outline", "schema", "spec", "manifest",
            "setup", "plan", "evidence map", "validation checks", "merge order", "ownership",
            "框架", "契约", "大纲", "规格", "清单", "证据", "验收",
        )
    )
    if not has_contract_role:
        return False

    has_substantive_body = any(
        marker in prompt_l
        for marker in (
            "implement all", "write all code", "full implementation", "complete implementation",
            "run benchmark", "run experiments", "generate final docx", "assemble final paper",
            "write paper body", "write final report", "benchmark results", "final numeric table",
            "实现全部", "完整实现", "运行基准", "运行实验", "生成最终", "输出word",
            "输出 word", "写论文正文", "撰写论文正文", "写最终报告", "算法实现正文",
        )
    )
    if has_substantive_body:
        owns_only_structural_outputs = bool(outputs) and all(
            output in validation_scripts or output.endswith((".md", ".txt", ".json"))
            for output in lowered_outputs
        )
        prompt_limits_scope = any(
            marker in prompt_l
            for marker in (
                "do not implement", "no implementation", "only a compact", "contract only",
                "framework only", "structure only", "later helpers", "no benchmark results",
                "no final report", "不实现", "不要实现", "不写算法实现", "不写论文正文",
                "只写框架", "仅框架", "只写结构", "只写结构和槽位", "后续 helper",
            )
        )
        if not (owns_only_structural_outputs and prompt_limits_scope):
            return False
    return True


def _looks_like_framework_producer(task: dict) -> bool:
    text = _task_text(task).lower()
    task_id = str(task.get("task_id") or "").lower()
    id_markers = (
        "framework",
        "contract",
        "spec",
        "schema",
        "outline",
        "inventory",
        "project_map",
        "harness",
        "benchmark_spec",
        "evidence_map",
        "框架",
        "契约",
        "规格",
        "大纲",
        "清单",
    )
    if any(marker in task_id for marker in id_markers):
        return True
    return bool(re.search(
        r"\b(create|build|define|design|draft|prepare|write|produce)\b.{0,80}"
        r"\b(framework|contract|spec|schema|outline|inventory|project map|benchmark spec|evidence map)\b",
        text,
        re.IGNORECASE,
    ) or re.search(
        r"(创建|建立|定义|设计|编写|产出|准备).{0,40}(框架|契约|规格|大纲|清单|证据地图)",
        text,
    ))


def _looks_like_framework_reference(task: dict) -> bool:
    text = _task_text(task).lower()
    return any(
        marker in text
        for marker in (
            "shared framework",
            "framework contract",
            "benchmark spec",
            "shared benchmark",
            "interface contract",
            "schema contract",
            "evidence map",
            "merge order",
            "contract.json",
            "interface.py",
            "统一框架",
            "共享框架",
            "框架契约",
            "统一测量",
            "证据地图",
            "合并顺序",
        )
    )


def _count_independent_units(text: str) -> int:
    """Count independent units of work from task text.

    计算任务文本中的独立工作单元数。
    """
    lower = text.lower()
    known_names = {
        "red-black tree",
        "red black tree",
        "rbtree",
        "rb tree",
        "红黑树",
        "skip list",
        "skiplist",
        "跳表",
        "b-tree",
        "btree",
        "b树",
        "b 树",
        "b+tree",
        "b+ tree",
        "b+树",
        "b+ 树",
        "bptree",
        "lsm",
        "lsm树",
        "fractal tree",
        "分形树",
        "avl",
        "avl树",
        "hash index",
        "哈希索引",
    }
    count = sum(1 for name in known_names if name in lower)

    # Detect multiple work types mentioned together
    work_types = []
    if re.search(r"\b(framework|contract|spec|schema)s?\b", lower):
        work_types.append("framework")
    if re.search(r"\b(algorithm|implementation|module|component)s?\b", lower):
        work_types.append("implementation")
    if re.search(r"\b(benchmark|performance|experiment|comparison)s?\b", lower):
        work_types.append("benchmark")
    if re.search(r"\b(document|paper|report|analysis)\b", lower):
        work_types.append("document")
    if re.search(r"\b(test|verification|validation)s?\b", lower):
        work_types.append("test")

    # Multiple work types suggest multiple units
    if len(work_types) >= 3:
        count = max(count, len(work_types))

    chinese_algorithm_list = re.search(
        r"(?:分析|比较|实现|研究|撰写).{0,30}(?:四种|五种|多种|若干|几个).{0,20}(?:算法|数据结构|结构)",
        text,
    )
    if chinese_algorithm_list:
        count = max(count, 3)
    if any(marker in text for marker in ("每种算法", "每个算法", "各算法", "各数据结构")) and any(
        marker in text for marker in ("比较表", "对比表", "统一表格", "论文", "章节", "素材")
    ):
        count = max(count, 3)
    headings = re.findall(r"^\s*(?:#{2,6}|\d+[.)、]|[-*•])\s+(.{2,80})$", text, re.MULTILINE)
    plausible = [
        h for h in headings
        if not any(skip in h.lower() for skip in ("acceptance", "requirement", "output", "验收", "要求", "输出"))
    ]
    return max(count, len(plausible))


def _task_breadth(task: dict) -> dict:
    text = _task_text(task)
    outputs = task.get("expected_outputs") or []
    output_basenames = [os.path.basename(str(x).replace("\\", "/")).lower() for x in outputs]
    source_or_data_outputs = [
        name for name in output_basenames
        if name.endswith((".py", ".c", ".cpp", ".h", ".hpp", ".js", ".ts", ".html", ".csv", ".json", ".md", ".docx", ".xlsx", ".png"))
    ]
    units = _count_independent_units(text)
    broad_signals = 0
    if len(text) >= 1800:
        broad_signals += 1
    if len(outputs) >= 4:
        broad_signals += 1
    if units >= 3:
        broad_signals += 1
    if units >= 3 and re.search(
        r"\b(chapter|section|paper|report|analysis|comparison|research)\b",
        text,
        re.IGNORECASE,
    ):
        broad_signals += 1
    if units >= 3 and any(word in text for word in ("章节", "论文", "报告", "分析", "比较", "研究")):
        broad_signals += 1
    if len(source_or_data_outputs) >= 4:
        broad_signals += 1
    if re.search(r"\b(compare|benchmark|performance|paper|report|implement|research)\b", text, re.IGNORECASE):
        broad_signals += 1
    if any(word in text for word in ("比较", "性能", "论文", "报告", "实现", "研究", "基准")):
        broad_signals += 1
    return {
        "text": text,
        "outputs": outputs,
        "output_basenames": output_basenames,
        "source_or_data_outputs": source_or_data_outputs,
        "units": units,
        "broad_signals": broad_signals,
    }


def broad_framework_guard_warnings(tasks: list[dict]) -> list[dict]:
    """Detect broad helper batches that should first use a shared framework."""
    warnings: list[dict] = []
    if not tasks:
        return warnings

    framework_producers = [t for t in tasks if _looks_like_framework_producer(t)]
    consumers = [t for t in tasks if t not in framework_producers]
    if consumers and framework_producers:
        missing_consumers = [t for t in consumers if not task_has_framework(t)]
        if missing_consumers:
            warnings.append({
                "task_id": "<batch>",
                "issue": "framework_producer_mixed_with_consumers",
                "severity": "high",
                "task_ids": [str(t.get("task_id") or "?") for t in missing_consumers[:20]],
                "observed_framework_dependency_fact": (
                    "A framework/spec producer is in the same batch as consumer helpers, and those consumers do not carry "
                    "a concrete `framework` field yet. Consumers need the compact contract as task-envelope evidence before "
                    "their outputs can be compared or merged reliably.\n\n"
                    "观察到框架生产者与消费者同批，消费者尚无 framework 字段；消费型 helper 需要框架契约事实。"
                ),
            })

    comparable: list[dict] = []
    oversized: list[dict] = []
    oversized_framework: list[dict] = []
    for task in tasks:
        kind = str(task.get("kind") or "").lower()
        if bool(task.get("resume")):
            continue
        if is_compact_framework_contract_task(task):
            continue
        b = _task_breadth(task)
        text = b["text"]
        output_basenames = b["output_basenames"]
        units = b["units"]
        source_or_data_outputs = b["source_or_data_outputs"]

        is_framework_producer = _looks_like_framework_producer(task)
        if (
            is_framework_producer
            and (
                len(text) >= 3500
                or len(source_or_data_outputs) >= 4
                or len(task.get("expected_outputs") or []) >= 6
                or text.count("```") >= 4
                or units >= 3
            )
        ):
            oversized_framework.append(task)

        if kind in {"code", "edit", "read", "verify", ""} and units >= 3 and not task_has_framework(task):
            comparable.append(task)
        elif kind in {"code", ""} and not task_has_framework(task):
            has_result_output = any(
                name.startswith("results_") and name.endswith((".csv", ".json", ".txt"))
                for name in output_basenames
            )
            has_comparison_signal = bool(
                re.search(r"\b(compare|benchmark|performance|experiment|metric|timing)\b", text, re.IGNORECASE)
                or any(word in text for word in ("比较", "性能", "基准", "实验", "指标", "计时"))
            )
            if has_result_output and has_comparison_signal:
                comparable.append(task)
        if b["broad_signals"] >= 3:
            oversized.append(task)

    if len(comparable) >= 3:
        warnings.append({
            "task_id": "<batch>",
            "issue": "missing_framework_for_peer_fanout",
            "severity": "high",
            "task_ids": [str(t.get("task_id") or "?") for t in comparable[:20]],
            "observed_framework_gap_fact": (
                "Several peer helpers appear to produce comparable slices, but their task envelopes do not include a shared "
                "`framework` field. A comparable fan-out needs common interfaces/schema, evidence map, validation checks, "
                "ownership, merge order, and output matrix facts: task_id, kind, mode, input_files, expected_outputs, and final merge/apply target. "
                "`_helpers_shared/...` is handoff evidence, not a final user-facing artifact.\n\n"
                "观察到同类分片缺少共享 framework 字段；横向可比工作需要共同接口、schema、验收和合并事实。"
            ),
        })

    for task in oversized_framework[:4]:
        warnings.append({
            "task_id": str(task.get("task_id") or "?"),
            "issue": "overconcentrated_framework_task",
            "severity": "high",
            "observed_framework_scope_fact": (
                "This framework helper envelope is large enough to include likely implementation, experiment, evidence, chart, "
                "or final-document work in addition to the structural contract. A framework contract is most reliable when it "
                "stays compact: inventory, interfaces, schemas, ownership boundaries, validation plan, slots, dependencies, "
                "acceptance, and downstream output matrix. `_helpers_shared/...` is handoff evidence; final deliverables need "
                "non-shared files or `_env/...` project paths.\n\n"
                "观察到框架任务可能混入实质产出；框架契约应保持结构化、紧凑、可传递。"
            ),
            "signals": {
                "prompt_chars": len(str(task.get("prompt") or "")),
                "expected_outputs": len(task.get("expected_outputs") or []),
                "code_block_count": str(task.get("prompt") or "").count("```"),
            },
        })

    for task in oversized[:8]:
        if task in oversized_framework:
            continue
        has_framework_contract = task_has_framework(task)
        looks_like_framework_reference = _looks_like_framework_reference(task)
        if has_framework_contract and not looks_like_framework_reference:
            framework_boundary_fact = (
                "This helper already has a framework contract, but the envelope still appears to own many weakly coupled "
                "responsibilities. Bounded slice boundaries may exist by module, chapter, algorithm, source range, data shard, "
                "experiment, or verification target. Final assembly/apply remains a separate explicit slice when the user asked "
                "for a report, document, or project files.\n\n"
                "观察到已有 framework 但职责仍集中；可能存在模块、章节、算法、数据或验证分片边界。"
            )
            observed_framework_state = "has_framework"
        else:
            framework_boundary_fact = (
                "This helper appears to own many weakly coupled responsibilities without a concrete framework field. "
                "A shared framework fact can make later module, chapter, data-shard, experiment, source-range, or verification-target "
                "slices comparable and mergeable. Long outputs are easier to inspect when produced as bounded segments.\n\n"
                "观察到职责集中且缺少 framework 字段；共享框架事实可让后续分片可比较、可合并。"
            )
            observed_framework_state = "missing_framework"
        warnings.append({
            "task_id": str(task.get("task_id") or "?"),
            "issue": "overconcentrated_helper_task",
            "severity": "high",
            "observed_framework_state": observed_framework_state,
            "observed_framework_boundary_fact": framework_boundary_fact,
            "signals": {
                "prompt_chars": len(str(task.get("prompt") or "")),
                "expected_outputs": len(task.get("expected_outputs") or []),
                "unit_count": _count_independent_units(_task_text(task)),
                "has_framework": has_framework_contract,
                "looks_like_framework_reference": looks_like_framework_reference,
            },
        })
    return warnings


def high_priority_framework_warnings(warnings: list[dict], *, trace_total: int = 0, cap: int = 2) -> list[dict]:
    """Return high-priority framework facts for the guard LLM.

    These facts no longer hard-block helper startup by themselves. Runtime
    callers pass them as model-visible guard observations; only the guard LLM
    may turn them into a hard intervention.

    框架事实只供守卫判断；符号化检测本身不执行硬拦截。
    """
    if trace_total >= cap:
        return []
    return [
        w for w in warnings
        if str(w.get("severity") or "").lower() == "high"
        and str(w.get("issue") or "") in {
            "framework_producer_mixed_with_consumers",
            "missing_framework_for_peer_fanout",
            "overconcentrated_framework_task",
            "overconcentrated_helper_task",
        }
    ]


def blocking_framework_warnings(warnings: list[dict], *, trace_total: int = 0, cap: int = 2) -> list[dict]:
    """Compatibility alias for older tests/tools; does not imply runtime blocking."""
    return high_priority_framework_warnings(warnings, trace_total=trace_total, cap=cap)
