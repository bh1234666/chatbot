"""工作区/路径工具:目录大小、文件枚举、产物归类、声明输出匹配、快照等。

2026-05-20 重构: 从 llm/tools/delegate.py 原样抽出。经 tools/extract_analysis.py
--closure 验证为自包含(14 函数, 0 unsafe),仅依赖 stdlib(os/subprocess/pathlib),
与 delegate 无循环依赖。delegate.py 通过 re-export 保持兼容,调用点零改动。
"""
import os
import re
import json
import subprocess as _subprocess
from pathlib import Path


_OFFICE_DELIVERABLE_EXTS = (".docx", ".pptx", ".xlsx", ".xlsm", ".pdf")


def _has_office_document_output(prompt: str, expected_outputs: list[str] | None = None) -> bool:
    outputs = "\n".join(str(x) for x in (expected_outputs or []))
    if any(s in outputs.lower() for s in _OFFICE_DELIVERABLE_EXTS):
        return True
    if expected_outputs:
        return False
    prompt_text = str(prompt or "")
    if not prompt_text:
        return False
    lower = prompt_text.lower()
    if not any(s in lower for s in _OFFICE_DELIVERABLE_EXTS):
        return False
    delivery_signals = (
        "generate", "create", "write", "save", "export", "deliver",
        "output", "produce", "assemble", "turn into", "draft into",
        "生成", "创建", "新建", "写入", "保存", "导出", "输出",
        "交付", "形成", "整理成", "撰写", "制作",
    )
    return any(signal in lower or signal in prompt_text for signal in delivery_signals)


def _is_internal_read_evidence_output(path: str) -> bool:
    norm = str(path or "").replace("\\", "/").strip().strip("`\"'")
    if not norm:
        return False
    low = norm.lower().lstrip("./")
    if low == "_env" or low.startswith("_env/"):
        return False
    if not low.endswith(".txt"):
        return False
    base = low.rsplit("/", 1)[-1]
    if any(marker in base for marker in ("probe", "placeholder", "index_only", "manifest")):
        return False
    evidence_markers = (
        "evidence", "extract", "extracted", "ocr", "read", "source",
        "coverage", "material", "materials", "transcript", "notes",
        "visible_text", "docx_content", "analysis", "audit", "review",
        "inspection", "findings", "summary",
    )
    return any(marker in low for marker in evidence_markers) or base.startswith((".helper_", "read_"))


def _internal_read_evidence_name_from_staged_path(path: str) -> str:
    """Return a sandbox evidence filename for staged `_env/...` evidence requests.

    Read helpers must not write evidence into `_env/`, because `_env/` is the
    staged project tree. When the main thread asks for `_env/read_evidence_*.txt`,
    keep the intended evidence basename but make it a helper-internal output.

    `_env/read_evidence_*.txt` 会转成 helper 内部证据文件名，避免污染项目暂存区。
    """
    norm = str(path or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm.lower().startswith("_env/") or not norm.lower().endswith(".txt"):
        return ""
    base = norm.rsplit("/", 1)[-1]
    base_low = base.lower()
    evidence_markers = (
        "read_evidence",
        "ocr_evidence",
        "source_evidence",
        "material_evidence",
        "materials_evidence",
        "extraction_evidence",
        "extract_evidence",
        "evidence_",
        "_evidence",
        "visible_text",
        "transcript",
        "docx_content",
        "analysis",
        "audit",
        "review",
        "inspection",
        "findings",
        "summary",
    )
    if any(marker in base_low for marker in evidence_markers):
        return base
    return ""


def _rewrite_staged_read_evidence_mentions(text: str) -> str:
    """Rewrite `_env/<evidence>.txt` mentions to helper-internal filenames.

    This is used only for read-helper contracts. It preserves source-material
    `_env/...` paths and rewrites evidence output paths so model-visible checks
    match the actual write boundary.

    只改 read helper 证据产物提法，不改源材料路径。
    """
    raw = str(text or "")
    if "_env/" not in raw:
        return raw

    def repl(match: re.Match[str]) -> str:
        value = match.group(0)
        name = _internal_read_evidence_name_from_staged_path(value)
        return name or value

    return re.sub(r"_env/[^\s`'\"<>|:;，。？！\])}]+\.txt", repl, raw)


def _read_evidence_verdict(text: str) -> dict:
    """Extract a compact read-helper coverage verdict from evidence text."""
    verdict = ""
    coverage_summary = ""
    coverage_fallback = ""
    needs_escalation = None
    line_ranges = ""
    for raw in (text or "").splitlines()[:120]:
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        low = line.lower()
        if not verdict and upper.startswith("VERDICT:"):
            value = upper.split(":", 1)[1].strip()
            for candidate in ("PASS", "FAIL", "PARTIAL"):
                if candidate in value:
                    verdict = candidate
                    break
        if not coverage_summary and (
            "coverage_summary" in low
            or low.startswith("coverage:")
        ):
            coverage_summary = line[:500]
        if not coverage_fallback and ("完整读取" in line or "fully read" in low or "complete" in low):
            coverage_fallback = line[:500]
        if needs_escalation is None and "needs_escalation" in low:
            if "false" in low or "否" in line:
                needs_escalation = False
            elif "true" in low or "是" in line:
                needs_escalation = True
        if not line_ranges and ("line_ranges" in low or "recommended line" in low or "行范围" in line):
            line_ranges = line[:300]
    if not verdict:
        low_all = (text or "").lower()
        if (
            "verdict: pass" in low_all
            or "coverage: complete" in low_all
            or "coverage_summary" in low_all and ("fully read" in low_all or "完整读取" in text)
            or "needs_escalation: false" in low_all
        ):
            verdict = "PASS"
        elif "verdict: partial" in low_all or "partial" in low_all or "部分" in text:
            verdict = "PARTIAL"
        elif "verdict: fail" in low_all or "abort_extract" in low_all:
            verdict = "FAIL"
    if not coverage_summary:
        coverage_summary = coverage_fallback
    return {
        "verdict": verdict or None,
        "coverage_summary": coverage_summary,
        "needs_escalation": needs_escalation,
        "line_ranges": line_ranges,
    }


def _collect_read_evidence_files(
    *,
    main_workspace: str | None,
    task_id: str,
    copied_paths: list[str] | set[str] | tuple[str, ...] | None = None,
    max_files: int = 20,
) -> list[str]:
    """Find read-helper evidence files promoted to the main workspace.

    Read helpers intentionally do not expose raw extraction files as user
    deliverables. This list is the stable evidence channel for the main process,
    collect/status summaries, and later synthesis after context folding.

    read helper 证据不是用户交付物；此清单供主进程和摘要机制稳定引用。
    """
    evidence: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        norm = str(rel or "").replace("\\", "/").strip()
        if not norm or norm in seen:
            return
        if _is_internal_read_evidence_output(norm):
            seen.add(norm)
            evidence.append(norm)

    for path in copied_paths or []:
        add(str(path))

    if main_workspace and task_id:
        full_report_candidate = ""
        try:
            for name in sorted(os.listdir(main_workspace)):
                full = os.path.join(main_workspace, name)
                if not os.path.isfile(full):
                    continue
                low = name.lower()
                tid_low = str(task_id).lower()
                if not low.endswith(".txt"):
                    continue
                if low == f".helper_{tid_low}_full_report.txt":
                    full_report_candidate = name
                    continue
                if low.startswith(tid_low):
                    add(name)
        except OSError:
            pass
        if not evidence and full_report_candidate:
            add(full_report_candidate)
    return evidence[:max_files]


def _build_read_evidence_summary(
    *,
    main_workspace: str | None,
    task_id: str,
    evidence_files: list[str] | None = None,
    report: str = "",
    max_files: int = 12,
) -> dict:
    """Build a compact authoritative coverage summary for read helpers."""
    files = list(evidence_files or [])
    if not files:
        files = _collect_read_evidence_files(
            main_workspace=main_workspace,
            task_id=task_id,
            copied_paths=[],
            max_files=max_files,
        )
    verdicts: list[dict] = []
    for rel in files[:max_files]:
        text = ""
        if main_workspace:
            path = os.path.join(main_workspace, rel.replace("/", os.sep))
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read(12000)
            except OSError:
                text = ""
        parsed = _read_evidence_verdict(text)
        verdicts.append({
            "file": rel,
            "verdict": parsed.get("verdict"),
            "coverage_summary": parsed.get("coverage_summary"),
            "needs_escalation": parsed.get("needs_escalation"),
            "line_ranges": parsed.get("line_ranges"),
        })
    if not verdicts and report:
        parsed = _read_evidence_verdict(report[:12000])
        if parsed.get("verdict") or parsed.get("coverage_summary"):
            verdicts.append({
                "file": f".helper_{task_id}_full_report.txt",
                "verdict": parsed.get("verdict"),
                "coverage_summary": parsed.get("coverage_summary"),
                "needs_escalation": parsed.get("needs_escalation"),
                "line_ranges": parsed.get("line_ranges"),
            })
    pass_count = sum(1 for item in verdicts if item.get("verdict") == "PASS")
    partial_count = sum(1 for item in verdicts if item.get("verdict") == "PARTIAL")
    fail_count = sum(1 for item in verdicts if item.get("verdict") == "FAIL")
    return {
        "evidence_files": files[:max_files],
        "verdicts": verdicts,
        "pass_count": pass_count,
        "partial_count": partial_count,
        "fail_count": fail_count,
        "has_complete_evidence": bool(verdicts) and pass_count > 0 and fail_count == 0 and partial_count == 0,
        "usage": (
            "Use these evidence files and coverage verdicts as source-reading facts. "
            "A successful read helper with PASS evidence should not be marked unread; "
            "a failed/partial helper must be named as a gap or resumed before full-coverage synthesis.\n\n"
            "PASS 证据表示该读取 helper 已覆盖；失败或部分覆盖需明确为缺口或继续处理。"
        ),
    }


def _is_source_material_reference(path: str) -> bool:
    low = str(path or "").replace("\\", "/").strip().strip("`\"'").lower().lstrip("./")
    if not low:
        return False
    source_exts = (
        ".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".mp3", ".wav",
        ".zip", ".rar", ".7z",
    )
    return low.endswith(source_exts)


def _filter_read_helper_expected_outputs(prompt: str, expected_outputs: list[str] | None = None) -> list[str]:
    """Keep only read-helper-owned evidence outputs.

    Read tasks often mention DOCX/PDF/image/audio/archive source files. Those
    paths are inputs, not helper deliverables; keeping them in expected_outputs
    makes kind guards bounce between read and edit/code. A read helper may own
    internal `.txt` evidence files only.

    read helper 的 expected_outputs 只保留内部 txt 证据；Office/PDF/图片等是源材料引用。
    """
    filtered: list[str] = []
    seen: set[str] = set()
    for raw in expected_outputs or []:
        value = str(raw or "").strip()
        if not value:
            continue
        staged_evidence_name = _internal_read_evidence_name_from_staged_path(value)
        if staged_evidence_name:
            key = staged_evidence_name.replace("\\", "/").lower()
            if key not in seen:
                seen.add(key)
                filtered.append(staged_evidence_name)
            continue
        if _is_internal_read_evidence_output(value):
            key = value.replace("\\", "/").lower()
            if key not in seen:
                seen.add(key)
                filtered.append(value)
    return filtered


def _derive_permanent_root(main_ws: str) -> str | None:
    """从 .temp/ 路径推导永久根 (P46 辅助)。

    入参典型: `<root>/<archive>/<group>/.temp` (主线程的 cwd)
    返回:     `<root>/<archive>/<group>`   (永久根, 跨会话保留)

    如果入参不是 .temp/ 路径, 返回 None (避免误操作)。
    """
    if not main_ws:
        return None
    _norm = main_ws.rstrip("/\\")
    if os.path.basename(_norm) != ".temp":
        return None  # 不是 .temp/, 不是双层结构
    _parent = os.path.dirname(_norm)
    if not _parent or not os.path.isdir(_parent):
        return None
    return _parent


def _extract_reported_output_files(report: str) -> list[str]:
    """Extract strictly formatted helper-declared output files.

    Helpers are instructed to report a JSON block such as
    `{"files": ["_env/out.md"]}` under an `Output files` report section. Disk
    recovery must not repair malformed helper reports by scraping arbitrary
    text; malformed format is evidence for retrying the helper/report, not
    evidence of success.

    磁盘恢复只信任规范输出区块；格式错误交给 LLM 续作修正。
    """
    text = str(report or "")
    section_match = re.search(
        r"(?is)(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:##\s*)?Output files(?:\*\*)?.{0,3000}?```(?:json)?\s*(\{.*?\"files\"\s*:\s*\[.*?\].*?\})\s*```",
        text,
    )
    if not section_match:
        return []
    raw = section_match.group(1)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return []
    candidates: list[str] = []
    for item in data.get("files") or []:
        if not isinstance(item, str):
            continue
        norm = item.replace("\\", "/").strip().strip("`\"'")
        if norm and norm not in candidates:
            candidates.append(norm)
    return candidates[:50]


def _has_malformed_output_files_attempt(report: str) -> bool:
    """Return true when a helper tried to declare files but missed the contract."""
    text = str(report or "")
    if not text:
        return False
    low = text.lower()
    if "output files" in low or '"files"' in text or "'files'" in text:
        try:
            return not bool(_extract_reported_output_files(text))
        except Exception:
            return True
    return False


def _resolve_reported_output_path(main_workspace: str, task_id: str, reported_path: str) -> str | None:
    norm = str(reported_path or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm or os.path.isabs(norm):
        return None
    if ".." in Path(norm).parts:
        return None

    candidates: list[str] = []
    candidates.append(os.path.join(main_workspace, norm.replace("/", os.sep)))
    base = os.path.basename(norm)
    if base:
        candidates.append(os.path.join(main_workspace, base))
        candidates.append(os.path.join(main_workspace, f"{task_id}_{base}"))
        candidates.append(os.path.join(main_workspace, f"helper_{task_id}_{base}"))
    for candidate in candidates:
        try:
            root = os.path.abspath(main_workspace)
            full = os.path.abspath(candidate)
            if not (full == root or full.startswith(root + os.sep)):
                continue
            if os.path.isfile(full):
                return os.path.relpath(full, root).replace(os.sep, "/")
        except OSError:
            continue
    return None


def _disk_result_for_collect(
    task_id: str, *, main_workspace: str | None,
) -> dict | None:
    if not main_workspace:
        return None
    try:
        report_path = os.path.join(
            main_workspace, f".helper_{task_id}_full_report.txt"
        )
        if not os.path.isfile(report_path):
            return None
        with open(report_path, "r", encoding="utf-8") as f:
            report = f.read()
    except OSError:
        return None
    reported_files = _extract_reported_output_files(report)
    malformed_output_files = not reported_files and _has_malformed_output_files_attempt(report)
    resolved_files: list[str] = []
    missing_files: list[str] = []
    for reported in reported_files:
        resolved = _resolve_reported_output_path(main_workspace, task_id, reported)
        if resolved:
            resolved_files.append(resolved)
        else:
            missing_files.append(reported)
    outputs_complete = bool(reported_files) and not missing_files
    if outputs_complete:
        terminal_reason = "disk_report_outputs_verified"
        quality_warnings: list[str] = []
    elif malformed_output_files:
        terminal_reason = "output_format_invalid"
        quality_warnings = [
            "The helper report attempted to declare output files but did not use the required Output files JSON block; resume the same task to fix the report or file declaration."
        ]
    else:
        terminal_reason = "unverified_disk_report"
        quality_warnings = [
            "Disk report recovery found no complete explicit output file set; inspect or resume before accepting."
        ]
    result = {
        "task_id": task_id,
        "ok": outputs_complete,
        "terminal_reason": terminal_reason,
        "report": report,
        "files": resolved_files,
        "outputs_check": {
            "outputs_complete": outputs_complete,
            "outputs_missing": missing_files,
            "quality_warnings": quality_warnings,
        },
        "recovered_from": "disk_full_report",
        "full_report_path": f".helper_{task_id}_full_report.txt",
        "reported_files": reported_files,
    }
    if malformed_output_files:
        result["retry_instruction"] = (
            "Resume the same helper task_id. Keep existing files, then finish with a compliant final report section: "
            "`## Output files` followed by a fenced JSON block exactly like "
            "```json\n{\"files\": [\"_env/relative_output.ext\"]}\n```. "
            "Keep the same task_id unless the workspace is unusable.\n\n"
            "同一 helper 续作，保留已有文件，并按规范补齐 Output files JSON。"
        )
        result["next_action"] = {"type": "resume_same_task_fix_output_format"}
    return result


def _dir_size(path: str) -> int:
    """递归计算目录大小(bytes)。Windows / Unix 通用,用于 fork 前预估。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            # 跳过 _delegate_* helper 子区(避免 fork 嵌套复制本身)
            if any(p.startswith("_delegate_") for p in Path(root).parts):
                continue
            for f in files:
                if f.startswith("."):
                    continue
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _validate_shared_scaffold(helper_workspace: str) -> str:
    """编译检查 _shared/ 中的 C 源文件,返回警告文本(空串=全部通过)。

    2026-05-05: 修复"主线程写的 _shared_ 脚手架有 bug 污染所有 helper"问题。
    在 helper 启动前用 gcc -fsyntax-only 快扫 _shared/*.c,发现语法错误立即
    注入提示让 helper 优先检查/修复,而不是盲目信之。
    """
    shared_dir = os.path.join(helper_workspace, "_shared")
    if not os.path.isdir(shared_dir):
        return ""
    c_files = [f for f in os.listdir(shared_dir) if f.endswith(".c")]
    if not c_files:
        return ""
    warnings: list[str] = []
    gcc = "gcc"
    for cf in sorted(c_files):
        path = os.path.join(shared_dir, cf)
        try:
            r = _subprocess.run(
                [gcc, "-fsyntax-only", "-Wall", "-std=c11", path],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                stderr_tail = r.stderr.strip()
                if len(stderr_tail) > 200:
                    stderr_tail = stderr_tail[-200:]
                warnings.append(f"  - {cf}: gcc -fsyntax-only FAILED\n    {stderr_tail}")
        except FileNotFoundError:
            return ""  # gcc 不可用,静默跳过
        except Exception:
            pass  # 超时等,跳过
    if not warnings:
        return ""
    warning_text = (
        "\n\n## Shared Scaffold Preflight Warning\n"
        "The following _shared/ files failed `gcc -fsyntax-only` before helper startup:\n"
        + "\n".join(warnings) +
        "\n\nInspect these files first when they are relevant. If a shared scaffold file is faulty, "
        "record the finding in the report and place any repair in _helpers_shared/ rather than "
        "editing the provided scaffold in place.\n\n"
        "启动后优先检查相关共享脚手架；修复放 _helpers_shared/，并在报告中说明。"
    )
    return warning_text


def _workspace_has_basename(*roots: str | None) -> set[str]:
    existing: set[str] = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for walk_root, walk_dirs, walk_files in os.walk(root):
                walk_dirs[:] = [
                    d for d in walk_dirs
                    if not d.startswith(".") and d not in {"__pycache__", ".git"}
                ]
                for walk_file in walk_files:
                    existing.add(walk_file.lower())
                    rel = os.path.relpath(os.path.join(walk_root, walk_file), root)
                    rel_posix = rel.replace("\\", "/")
                    existing.add(rel_posix.lower())
                    if rel_posix.startswith("_helpers_shared/"):
                        existing.add(rel_posix[len("_helpers_shared/"):].lower())
        except OSError:
            pass
    return existing


def _list_workspace_files(ws_dir: str) -> list[str]:
    """List all regular files in workspace, returning paths relative to ws_dir."""
    result: list[str] = []
    try:
        for root, dirs, files in os.walk(ws_dir):
            # Skip internal delegate directories
            dirs[:] = [d for d in dirs if not d.startswith("_delegate_") and not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                full = os.path.join(root, f)
                if os.path.isfile(full):
                    result.append(os.path.relpath(full, ws_dir))
    except OSError:
        pass
    return result


def _match_path_pattern(rel_path: str, pattern: str) -> bool:
    """Simple glob match for path patterns. Uses fnmatch for wildcard matching."""
    import fnmatch
    return fnmatch.fnmatch(rel_path, pattern)


def _extract_declared_files(report: str) -> set[str]:
    """从 helper 报告中提取声明的产出文件名。

    优先尝试 JSON 代码块（## 产出文件 段内的 ```json ... ```），
    回退到反引号包裹格式的文本解析。
    """
    import re as _re
    import json as _json
    declared: set[str] = set()
    if not report:
        return declared

    def _normalize_declared_name(raw: str) -> str | None:
        fname = str(raw or "").strip()
        if not fname:
            return None
        norm = fname.replace("\\", "/")
        if norm.startswith('_helpers_shared/'):
            norm = norm[len('_helpers_shared/'):]
        if '/' in norm:
            base = os.path.basename(norm)
            if not base or base.startswith('_'):
                return None
            if any(ch in base for ch in ('\n', '\r', '\t', ':', '：', '，', '。', '；')):
                return None
            if ' ' in base:
                return None
            return base
        fname = norm
        if fname.startswith('_'):
            return None
        if any(ch in fname for ch in ('\n', '\r', '\t', ':', '：', '，', '。', '；')):
            return None
        if ' ' in fname:
            return None
        return fname or None

    # ── 优先: JSON 解析(两种格式) ──
    # 格式1: ## 产出文件\n```json\n{"files": ["a.c", "b.txt"]}\n```
    # 格式2: 原始 JSON {"files": [...]}(helper 常忽略 markdown 格式要求)
    _json_block_re = _re.compile(
        r'##\s*产出文件\s*\n\s*```json\s*\n(\{.*?\})\s*\n\s*```',
        _re.DOTALL,
    )
    _jm = _json_block_re.search(report)

    # 如果 markdown 格式不匹配,尝试在整个 report 中找 {"files": [...]} JSON
    if not _jm:
        _raw_json_re = _re.compile(
            r'\{\s*"files"\s*:\s*\[.*?\]\s*\}',
            _re.DOTALL,
        )
        _jm = _raw_json_re.search(report)

    if _jm:
        try:
            data = _json.loads(_jm.group(1) if _jm.lastindex else _jm.group(0))
            files = data.get("files") or data.get("output_files") or []
            if isinstance(files, list):
                for f in files:
                    fname = _normalize_declared_name(f)
                    if fname:
                        declared.add(fname)
                if declared:
                    return declared
        except (_json.JSONDecodeError, ValueError, TypeError):
            pass  # JSON 解析失败,回退到文本解析

    # ── 回退: 反引号包裹格式的文本解析 ──
    # 先去掉 JSON/markdown 代码块,避免 ``` 被误匹配为文件反引号
    _section_re = _re.compile(
        r'(?:###?\s*(?:产出文件|关键文件|输出文件|交付文件|生成文件|文件清单)'
        r'|##\s*(?:关键文件|产出文件))',
    )
    _code_block_re = _re.compile(r'```[^`]*```', _re.DOTALL)
    _file_re = _re.compile(r'(?<![`])\*?\*?`([^`\n]+)`\*?\*?')
    for m in _section_re.finditer(report):
        start = m.end()
        next_section = _re.search(r'\n##?\s', report[start:])
        end = start + next_section.start() if next_section else len(report)
        section_text = report[start:end]
        # 剥离代码块,防止 ``` 干扰
        clean_text = _code_block_re.sub(' ', section_text)
        for fm in _file_re.finditer(clean_text):
            fname = _normalize_declared_name(fm.group(1))
            if fname:
                declared.add(fname)
    return declared


def _is_internal_helper_artifact(path: str) -> bool:
    norm = str(path or "").replace("\\", "/").strip()
    if not norm:
        return False
    base = os.path.basename(norm)
    if base in {".session_tag", ".read_history.json", ".todos.json", ".edit_history.json"}:
        return True
    if base.startswith(".") and base.endswith(("_call_count.json", "_history.json", "_count.json", "_rewrite_count.json", ".todos_call_count.json")):
        return True
    if base.endswith(("_call_count.json", "_history.json", "_count.json", "_rewrite_count.json", ".todos_call_count.json")):
        return True
    if norm.startswith("_helpers_shared/"):
        rel = norm[len("_helpers_shared/"):]
        if not rel:
            return True
        if rel.startswith("."):
            return True
        if "/" not in rel and os.path.splitext(base)[1].lower() in {".py", ".pyw", ".pyc", ".pyo"}:
            return True
    return False


def _is_shared_support_artifact(path: str) -> bool:
    norm = str(path or "").replace("\\", "/").strip()
    if not norm.startswith("_helpers_shared/"):
        return False
    if _is_internal_helper_artifact(norm):
        return True
    base = os.path.basename(norm).lower()
    ext = os.path.splitext(base)[1]
    return ext in {".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".txt"}


def _matches_declared_output_via_mapping(path: str, declared_set: set[str], file_map: list[dict]) -> bool:
    norm = str(path or "").replace("\\", "/").strip()
    if not norm:
        return False
    base = os.path.basename(norm)
    for declared in declared_set or set():
        declared_norm = str(declared or "").replace("\\", "/").strip()
        if not declared_norm:
            continue
        declared_base = os.path.basename(declared_norm)
        if norm == declared_norm or base == declared_base or norm.endswith("_" + declared_norm):
            return True
    for entry in file_map or []:
        if not isinstance(entry, dict):
            continue
        helper_name = str(entry.get("helper_name") or "").replace("\\", "/").strip()
        main_name = str(entry.get("main_name") or "").replace("\\", "/").strip()
        shared_name = str(entry.get("shared_name") or "").replace("\\", "/").strip()
        mapped_names = {name for name in (helper_name, main_name, shared_name) if name}
        if not mapped_names:
            continue
        mapped_bases = {os.path.basename(name) for name in mapped_names}
        if norm not in mapped_names and base not in mapped_bases:
            continue
        for declared in declared_set or set():
            declared_norm = str(declared or "").replace("\\", "/").strip()
            if not declared_norm:
                continue
            declared_base = os.path.basename(declared_norm)
            if declared_norm in mapped_names or declared_base in mapped_bases:
                return True
            if any(name.endswith("_" + declared_norm) for name in mapped_names):
                return True
            if shared_name and declared_norm.startswith("_helpers_shared/") and shared_name == declared_norm:
                return True
            if helper_name and declared_base == os.path.basename(helper_name):
                return True
    return False


# 2026-05-15 P115: helper spawn 时注入工作区清单
def _list_helper_workspace_for_prompt(ws_dir: str, max_files: int = 30) -> str:
    """列出 helper 沙箱里的文件 (含 _shared 和 _helpers_shared 子目录)。
    返回格式化字符串注入 sys_prompt 末尾, helper 启动就知道工作区有啥。
    病因: helper 刚启动通常先 search_files / workspace.list 探索 (浪费 1-2 iter)。
    """
    if not ws_dir or not os.path.isdir(ws_dir):
        return ""
    try:
        root_files = []
        for entry in sorted(os.listdir(ws_dir)):
            full = os.path.join(ws_dir, entry)
            if entry.startswith('.'):
                continue
            if os.path.isfile(full):
                try:
                    size = os.path.getsize(full)
                    size_str = f"{size}B" if size < 1024 else f"{size//1024}KB"
                    root_files.append(f"  {entry} ({size_str})")
                except OSError:
                    root_files.append(f"  {entry}")
            elif os.path.isdir(full):
                # 子目录: 仅展开 _shared / _helpers_shared / group_files
                if entry in ("_shared", "_helpers_shared", "group_files"):
                    try:
                        sub_entries = sorted(os.listdir(full))[:10]
                        if sub_entries:
                            root_files.append(f"  {entry}/  ({len(sub_entries)} items)")
                            for sf in sub_entries[:5]:
                                root_files.append(f"    {entry}/{sf}")
                            if len(sub_entries) > 5:
                                root_files.append(f"    ... {len(sub_entries)-5} more items")
                    except OSError:
                        pass
                else:
                    root_files.append(f"  {entry}/")

        if not root_files:
            return (
                "\n\n## Helper Workspace Snapshot\n"
                "The helper sandbox is empty. This is a fresh task with no staged artifacts yet. "
                "If the main prompt references an existing file, call fetch_to_temp(source='main', paths=[...]) "
                "for main-workspace inputs, or report the exact missing project path when an `_env/...` copy is required.\n\n"
                "helper 沙箱为空；需要已有文件时先获取到本地副本。"
            )

        truncate = ""
        if len(root_files) > max_files:
            root_files = root_files[:max_files]
            truncate = "\n  ... (use search_files for the full list)"

        return (
            "\n\n## Helper Workspace Snapshot\n"
            "These files are already staged in the helper sandbox. Use them directly when relevant; "
            "do not regenerate existing inputs. Fetch missing main-workspace files with fetch_to_temp.\n\n"
            "```\n" + "\n".join(root_files) + truncate + "\n```\n"
            "已在沙箱的文件直接使用；缺主区文件时 fetch_to_temp。"
        )
    except Exception:
        return ""


def take_workspace_snapshot(ws_dir: str) -> dict[str, tuple[float, int]]:
    """Take a (name → mtime+size) snapshot of all files in a workspace.

    用于 helper fork 完成后拍快照,helper 跑完时 diff 出真正新增/修改的文件
    (而不是把 fork 时已经从主区带过来的文件也当成 helper 产出复制回主区)。

    这是 B1 修复的核心 — 之前的 _copy_results_to_main 不区分"helper 产出的文件"
    和"fork 时从主区带过来的文件",于是把所有非源码文件都加 task_id_ 前缀复制回主区。
    叠加多 helper fork-copy 链 → 文件名出现 5/6 层 task_id 前缀,主区指数膨胀。

    2026-05-15 P64 修订:同时拍 _helpers_shared/<subdir>/ 下的文件快照, key 为
    posix 相对路径 (如 "_helpers_shared/radix_bench/radix_sort.c")。
    病因(实测 16:28 trace): _helpers_shared/ 跨会话残留(如上一轮的 radix_bench/)
    被错认为本次 helper 产出, "已交付到主区"列表里混入了无关文件污染主线程判断。
    修法: shared 区文件也参与 fork-snapshot diff, 只有真正新增/修改才算本次 helper 产出。
    """
    snap: dict[str, tuple[float, int]] = {}
    if not ws_dir or not os.path.isdir(ws_dir):
        return snap
    try:
        for name in os.listdir(ws_dir):
            full = os.path.join(ws_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
                snap[name] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    except OSError:
        pass
    # 2026-05-15 P64: 也拍 _helpers_shared/<subdir>/* 快照(深度 2)
    helpers_shared_dir = os.path.join(ws_dir, "_helpers_shared")
    if os.path.isdir(helpers_shared_dir):
        try:
            for root, _dirs, files in os.walk(helpers_shared_dir):
                for sf in files:
                    full = os.path.join(root, sf)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, ws_dir).replace(os.sep, "/")
                    snap[rel] = (st.st_mtime, st.st_size)
        except OSError:
            pass
    env_dir = os.path.join(ws_dir, "_env")
    if os.path.isdir(env_dir):
        try:
            for root, _dirs, files in os.walk(env_dir):
                for sf in files:
                    full = os.path.join(root, sf)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, ws_dir).replace(os.sep, "/")
                    snap[rel] = (st.st_mtime, st.st_size)
        except OSError:
            pass
    return snap
