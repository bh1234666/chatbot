"""工作区结果/提示文本工具:测试输出摘要抽取、命令模式比对、缺失文件 fetch 提示、
结构化读取拒绝信息。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。closure 自包含(4 函数, 0 unsafe),
仅依赖 stdlib(os/re)。workspace.py re-export 兼容,调用点零改动。
"""
import os
import re


def _helper_missing_file_fetch_hint(ws_dir: str, path: str) -> str:
    if "_delegate_" not in (ws_dir or ""):
        return ""
    norm_path = str(path or "").replace("\\", "/").strip()
    if not norm_path or norm_path.startswith(("_helpers_shared/", "_shared/", ".prev/", ".temp/")):
        return ""
    if norm_path == "_env" or norm_path.startswith("_env/"):
        return (
            "\n-> This helper can use project files only after the main process stages them under `_env/...`."
            f" The requested `_env` copy is missing: `{norm_path}`."
            " First inspect `_env/project_inventory.md` or `_env/.resource_manifest.json` if present, then check "
            "helper-result paths supplied by the main process (file_map, main_available_files, "
            "copy_stats.env_copied_files, internal_evidence_files) and locate/search in the sandbox. "
            "If the manifest lists the exact project_path, call fetch_to_temp(source='main', paths=[project_path]) "
            "once. If absent after that, call request_resource with that exact project_path and resume condition. "
            "Stay with staged relative paths and exact manifest project paths; use `_helpers_shared/...` only when it is explicitly exposed."
            "\n项目文件需先看资源清单和结果映射；未暂存时按精确 project_path 获取，仍缺则 request_resource。"
        )
    if "/" in norm_path or norm_path.startswith("."):
        return (
            "\n-> This is a helper sandbox path. Before repeating this read, inspect `_suggestions`, use "
            "workspace(action='locate'), and check any helper-result file_map/main_available_files/copy_stats "
            "paths provided by the main process. If the path is a future same-batch producer output, request "
            "that resource instead of guessing another prefix."
            "\nhelper 沙箱路径缺失时，先看建议、locate 和主进程提供的结果映射；同批产物未就绪则请求资源，不要猜路径。"
        )
    return (
        "\n-> This is a helper sandbox. If this file is an existing main-workspace input, "
        f"call fetch_to_temp(source='main', paths=['{norm_path}']) before reading it by the same relative name. "
        "Use `_helpers_shared/...` only when that exact shared path is exposed, and keep reads workspace-relative."
        "\nhelper 沙箱缺主区文件时，先 fetch_to_temp 到本地同名路径；只使用清单或结果明确暴露的路径。"
    )


def _structured_read_file_rejection(path: str) -> dict | None:
    ext = os.path.splitext(path)[1].lower()
    office_exts = {".docx", ".pptx", ".xlsx", ".xlsm"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
    media_exts = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".amr", ".mp4", ".mov", ".avi", ".webm"}
    archive_exts = {".zip", ".7z", ".rar", ".tar", ".gz"}
    executable_exts = {".exe", ".dll", ".so", ".dylib", ".bin"}

    if ext in office_exts:
        return {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": path,
            "file_category": "document" if ext in {".docx", ".pptx"} else "spreadsheet",
            "message": (
                "Office files are structured documents, not plain text. Inspect the file structure first. "
                "Use office(action='read') for body text, and office append/replace/update actions for edits.\n"
                "Office 文件需用 inspect_file/office 工具读取或编辑。"
            ),
            "suggested_tools": ["inspect_file", "office read", "office append", "office replace/update"],
        }
    if ext == ".pdf":
        return {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": path,
            "file_category": "document",
            "message": (
                "PDF files are structured documents, not plain text. Inspect the file first, then extract text "
                "or use OCR for scanned/visual pages according to the inspection result.\n"
                "PDF 先 inspect_file，再按结构提取文本或 OCR。"
            ),
            "suggested_tools": ["inspect_file", "pdf extraction", "ocr for scanned pages"],
        }
    if ext in image_exts:
        return {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": path,
            "file_category": "image",
            "message": (
                "Images are not plain text. Use inspect_file for dimensions/format, and OCR or visual reading "
                "when visible text or visual evidence is needed.\n"
                "图片需用 inspect_file 或 OCR/视觉读取。"
            ),
            "suggested_tools": ["inspect_file", "ocr", "vision/image inspection"],
        }
    if ext in media_exts:
        return {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": path,
            "file_category": "media",
            "message": (
                "Audio/video files are not plain text. Transcribe, extract frames, or use the appropriate media "
                "tool before making content claims.\n"
                "音视频需先转写、抽帧或使用专用工具。"
            ),
            "suggested_tools": ["inspect_file", "transcription", "frame extraction"],
        }
    if ext in archive_exts | executable_exts:
        return {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": path,
            "file_category": "archive" if ext in archive_exts else "binary",
            "message": (
                "This file is not plain text. Inspect it first, then use a format-specific extraction or handling "
                "tool when content is required.\n"
                "非纯文本文件先 inspect_file，再用对应格式工具。"
            ),
            "suggested_tools": ["inspect_file"],
        }
    return None


def _extract_test_summary(stdout: str, stderr: str) -> str | None:
    """从 stdout/stderr 提取测试结果的关键信号。

    匹配模式(按优先级):
    1. 测试框架格式:"=== Results: 1 pass, 8 fail ===" / "X passed, Y failed"
    2. 单元测试报告:"OK (5 tests)" / "FAILED (failures=2)" (Python unittest)
    3. pytest:"=== 5 passed in 1.23s ==="
    4. 单测失败行 — 但**仅当**已有正向 Results/pytest 信号或没有正向信号时才扫
    5. Python traceback

    2026-05-02 part16 修:_extract_test_summary 误报修复
      - 描述性句子 "This will fail" / "may fail" / "should fail" 不当作测试 fail
      - "=== Results: 5 pass, 0 fail ===" 全 pass 时**不**再扫 Failures(整行含 'fail' 是误报)
      - "FAIL" 必须是大写词或 colon-prefixed("FAILED:" / "Test 5: FAIL") 才算
      - "fail" 小写必须有数字上下文("8 fail" / "fail count=" 等)

    返回紧凑摘要字符串(< 500 字符);找不到 → None。
    """
    if not stdout and not stderr:
        return None
    text = (stdout or "") + "\n" + (stderr or "")

    findings: list[str] = []
    has_positive_or_explicit_signal = False  # 是否已找到明确的测试结果信号
    all_pass = False  # 所有测试都过 → 不该扫 Failures

    # 1. "Results: X pass, Y fail" 格式
    m = re.search(
        r'(?:===\s*)?Results?\s*:\s*(\d+)\s+pass(?:ed)?,?\s*(\d+)\s+fail(?:ed)?(?:\s*,\s*(\d+)\s+total)?',
        text, re.IGNORECASE,
    )
    if m:
        p, f = int(m.group(1)), int(m.group(2))
        total = int(m.group(3)) if m.group(3) else (p + f)
        verdict = "ALL PASS ✓" if f == 0 else f"{f} FAILED ✗"
        findings.append(f"[Test Results] {p}/{total} pass, {verdict}")
        has_positive_or_explicit_signal = True
        all_pass = (f == 0)

    # 2. Python unittest:"OK (5 tests)" / "FAILED (failures=2, errors=1)"
    if not findings:
        m = re.search(r'^(OK|FAILED)(?:\s*\([^)]*\))?\s*$', text, re.MULTILINE)
        if m:
            findings.append(f"[unittest] {m.group(0).strip()}")
            has_positive_or_explicit_signal = True
            all_pass = m.group(1) == "OK"

    # 3. pytest:"=== 5 passed in 1.23s ===" / "=== 1 failed, 4 passed ==="
    if not findings:
        m = re.search(
            r'(?:={3,}\s*)?((?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)(?:,\s*)?)+(?:\s+in\s+[0-9.]+s)?)',
            text,
            re.IGNORECASE,
        )
        if m:
            inner = m.group(1).strip()
            findings.append(f"[pytest] {inner}")
            has_positive_or_explicit_signal = True
            # 检查是不是全 pass(没出现 failed/error)
            all_pass = "failed" not in inner.lower() and "error" not in inner.lower()

    # 4. 单测失败行 — 仅当不是 all_pass 时才扫(避免在全 pass 情况下把"=== Results: 5 pass, 0 fail ==="行
    # 重新当成 fail 模式)
    if not all_pass:
        fail_lines = []
        # 4a. 语义清晰的 fail 模式(round-trip / mismatch / assertion 等)
        explicit_patterns = [
            r'^.{0,80}?(?:length\s+mismatch|decompress\s+returned\s+0|assertion\s+failed|expected\s+\S+\s+got\s+\S+).{0,100}',
        ]
        # 4b. FAIL/FAILED — 全大写词或 colon prefix
        # 例 OK: "FAIL: test_foo" / "Test 5: FAIL" / "✗ FAIL"
        # 例 不该匹配: "This will fail" / "may fail with" / "fail count="
        explicit_patterns.append(
            r'^.{0,80}?(?:^|\s|[:✗])(?:FAIL(?:ED)?(?::|\s+test|$|\s*[\w]+\s*$)).{0,100}'
        )
        # 4c. "N fail(ed)" 数字 + fail 词(典型测试计数)
        explicit_patterns.append(
            r'^.{0,80}?\b\d+\s+fail(?:ed|ures?)?\b.{0,100}'
        )
        for pattern in explicit_patterns:
            for m in re.finditer(pattern, text, re.MULTILINE):
                line = m.group(0).strip()
                # 排除 "Results: X pass, Y fail ===" 行(已被 (1) 处理)
                if re.search(r'Results?\s*:\s*\d+\s+pass', line, re.IGNORECASE):
                    continue
                # 排除描述性句子(常见模式:will/may/should/might/can fail)
                if re.search(r'\b(will|may|should|might|can|could|would)\s+fail\b', line, re.IGNORECASE):
                    continue
                # 排除"How to fail"/"if you fail"等指令式句子
                if re.search(r'\b(how to|if you|when you|to)\s+\S{0,15}\s*fail', line, re.IGNORECASE):
                    continue
                if line and len(fail_lines) < 5:
                    if not any(line[:40] == f[:40] for f in fail_lines):
                        fail_lines.append(line[:160])
        if fail_lines and (has_positive_or_explicit_signal or len(fail_lines) >= 1):
            # 仅当已有 Results 信号(带数据补强)或本身有失败行时才报
            findings.append(f"[Failures] {len(fail_lines)} pattern(s):\n  " + "\n  ".join(fail_lines))

    # 5. Python traceback 末行(最常见的有效信号)
    tb = re.search(r'^([A-Z]\w+(?:Error|Exception)):\s*(.+)$', text, re.MULTILINE)
    if tb and not findings:
        findings.append(f"[Python Exception] {tb.group(0).strip()[:180]}")

    if not findings:
        return None

    summary = " | ".join(findings)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    return summary


def _same_pattern(a: str, b: str) -> bool:
    """两行是否"模式相同" — 把数字/十六进制/标识符占位符化后比较。

    例: "  sym=0x42 freq=2 code_len=2" 和 "  sym=0x55 freq=2 code_len=1" → True
       "DEBUG huff_encode: input=6" 和 "DEBUG huff_encode: input=258" → True
       "INFO" 和 "ERROR" → False
    """
    if not a or not b:
        return False
    if abs(len(a) - len(b)) > 20:
        return False
    # 占位化:数字 → N, 十六进制 → X, 字母数字 → 单 token
    def normalize(s):
        s = re.sub(r'0x[0-9a-fA-F]+', 'X', s)
        s = re.sub(r'\b\d+(?:\.\d+)?\b', 'N', s)
        return s
    return normalize(a) == normalize(b)
