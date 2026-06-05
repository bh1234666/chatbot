"""Model-visible prompt hygiene.

Three layers of protection:

1. Constant-level (importlib): explicitly named modules expose prompt
   constants; each must be UTF-8 clean, bridge-agnostic, and carry a concise
   Chinese operator summary.
2. Coverage-level (AST module scan): module-level prompt constants must live in
   modules listed in ``PROMPT_MODULES`` so constant hygiene and duplicate checks
   cannot be bypassed by adding a new prompt module.
3. Source-level (AST auto-discovery): the whole ``app/`` tree is parsed and
   every inline model-visible prompt string is found automatically, so a new
   prompt added in any file is covered without updating a manual list.

Design intent across the codebase: English text is the model-facing source of
truth and every substantial prompt appends a short Chinese summary. These tests
keep that invariant enforceable in CI.
"""
from __future__ import annotations

import ast
import importlib
import re
from collections import defaultdict
from pathlib import Path
from types import ModuleType


# ── Shared markers ────────────────────────────────────────────────
# Bridge-specific names and classic mojibake bytes that must never reach a
# model-visible prompt. The bridge layer is an implementation detail; prompts
# stay transport-agnostic.
BAD_MARKERS = (
    "QQ",
    "NapCat",
    "napcat",
    "qqbridge",
    "\ufffd",
    "\u00c3",
    "\u00c2",
    "\u00e6",
    "\u00e7",
    "\u9225",
    "\u934f",
    "\u7ecb",
    "\u9429",
    "\u704f",
    "\u71ba",
    "\u7d2a",
    "\u6960",
)

# Three or more consecutive ASCII question marks signal Chinese text that was
# lossily re-encoded into '?' at some point in its history.
_LOSSY_QMARK = re.compile(r"\?{3,}")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _scan_text(text: str) -> list[str]:
    """Return marker/lossy issues found in a single prompt string."""
    issues: list[str] = []
    for marker in BAD_MARKERS:
        if marker in text:
            issues.append(f"contains {marker!r}")
    if _LOSSY_QMARK.search(text):
        issues.append("contains lossy '???' run (corrupted Chinese)")
    return issues


# ── Layer 1: explicit constant modules ────────────────────────────
PROMPT_MODULES = [
    "app.core.debug",
    "app.core.environment_prompt",
    "app.core.intermediate_feedback",
    "app.core.round_prompts",
    "app.core.orchestrator_prompts",
    "app.core.guard_prompts",
    "app.core.user_profile_maintenance",
    "app.memory.prompt_catalog",
    "app.core.dream.prompt_catalog",
    "app.llm.client",
    "app.llm.json_utils",
    "app.llm.aux_prompts",
    "app.llm.tools.delegate",
    "app.llm.tools.helper_prompt_catalog",
    "app.llm.tools.helper_prompts",
    "app.llm.tools.runtime_hints",
    "app.llm.voice_output",
]


_PROMPT_CONSTANT_NAME_KEYWORDS = (
    "PROMPT",
    "SYSTEM",
    "INSTRUCTION",
    "DIRECTIVE",
    "CONTRACT",
    "RULES",
    "GUIDANCE",
    "ADDON",
    "HINT",
    "TEMPLATE",
)


def _is_prompt_constant(name: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if name.startswith("__"):
        return False
    upper_name = name.upper()
    return name.startswith("ROUND") or any(
        keyword in upper_name for keyword in _PROMPT_CONSTANT_NAME_KEYWORDS
    )


def _module_name_from_app_path(path: Path) -> str:
    rel = path.with_suffix("").relative_to(_APP_PKG.parent)
    return ".".join(rel.parts)


def _static_prompt_constant_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return _concat_string(node)
    return ""


def _iter_module_level_prompt_constant_modules():
    roots = [
        _APP_PKG / "api",
        _APP_PKG / "core",
        _APP_PKG / "llm",
        _APP_PKG / "memory",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(_APP_PKG.parent).as_posix()
            if "/__pycache__/" in f"/{rel}" or rel.startswith("app/tests/"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                targets: list[str] = []
                value: ast.AST | None = None
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets.extend(t.id for t in node.targets if isinstance(t, ast.Name))
                elif isinstance(node, ast.AnnAssign):
                    value = node.value
                    if isinstance(node.target, ast.Name):
                        targets.append(node.target.id)
                if value is None:
                    continue
                if not any(
                    any(keyword in target.upper() for keyword in _PROMPT_CONSTANT_NAME_KEYWORDS)
                    or target.startswith("ROUND")
                    for target in targets
                ):
                    continue
                text = _static_prompt_constant_text(value)
                if len(text.strip()) < 40:
                    continue
                yield _module_name_from_app_path(path), path, tuple(targets)


def test_module_level_prompt_constants_are_in_explicit_prompt_modules():
    """New prompt modules must enter constant-level hygiene and duplicate checks."""
    known = set(PROMPT_MODULES)
    discovered: set[str] = set()
    failures: list[str] = []
    for module_name, path, targets in _iter_module_level_prompt_constant_modules():
        discovered.add(module_name)
        if module_name not in known:
            failures.append(f"{path.as_posix()} defines {', '.join(targets)}")

    assert discovered
    assert failures == []


def test_model_visible_prompt_constants_are_bridge_agnostic_and_utf8_clean():
    checked: list[str] = []
    failures: list[str] = []
    for module_name in PROMPT_MODULES:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if not _is_prompt_constant(name, value):
                continue
            checked.append(f"{module_name}.{name}")
            for issue in _scan_text(value):
                failures.append(f"{module_name}.{name} {issue}")
            if not _has_cjk(value):
                failures.append(f"{module_name}.{name} is missing concise Chinese operator summary")
    assert checked
    assert failures == []


def test_prompt_constant_detection_covers_templates_and_guidance_names():
    assert _is_prompt_constant("TASK_QUALITY_GUARD_USER_TEMPLATE", "English\n\n中文摘要")
    assert _is_prompt_constant("ENVIRONMENT_GUIDANCE", "English\n\n中文摘要")
    assert _is_prompt_constant("TOOL_RULES", "English\n\n中文摘要")
    assert not _is_prompt_constant("ORDINARY_CONSTANT", "English\n\n中文摘要")


# ── Tool-schema descriptions ──────────────────────────────────────
def test_tool_schema_descriptions_are_bridge_agnostic_and_utf8_clean():
    from app.llm.tools import tool_schemas

    checked: list[str] = []
    failures: list[str] = []
    for name, schema in vars(tool_schemas).items():
        if not (name.endswith("_SCHEMA") and isinstance(schema, dict)):
            continue
        function = schema.get("function") or {}
        desc = str(function.get("description") or "")
        if desc:
            checked.append(f"{name}.description")
            for issue in _scan_text(desc):
                failures.append(f"{name}.description {issue}")
        props = ((function.get("parameters") or {}).get("properties") or {})
        for prop_name, prop_schema in props.items():
            prop_desc = str(prop_schema.get("description") or "")
            if not prop_desc:
                continue
            checked.append(f"{name}.{prop_name}.description")
            for issue in _scan_text(prop_desc):
                failures.append(f"{name}.{prop_name}.description {issue}")
            if not _has_cjk(prop_desc):
                failures.append(f"{name}.{prop_name}.description is missing concise Chinese operator summary")
    assert checked
    assert failures == []


def _normalize_prompt_for_duplicate_check(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _iter_explicit_model_visible_prompt_texts():
    for module_name in PROMPT_MODULES:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if not _is_prompt_constant(name, value):
                continue
            yield f"{module_name}.{name}", value

    from app.llm.tools import tool_schemas

    for name, schema in vars(tool_schemas).items():
        if not (name.endswith("_SCHEMA") and isinstance(schema, dict)):
            continue
        function = schema.get("function") or {}
        desc = str(function.get("description") or "")
        if desc:
            yield f"app.llm.tools.tool_schemas.{name}.description", desc
        props = ((function.get("parameters") or {}).get("properties") or {})
        for prop_name, prop_schema in props.items():
            prop_desc = str(prop_schema.get("description") or "")
            if prop_desc:
                yield (
                    f"app.llm.tools.tool_schemas.{name}.{prop_name}.description",
                    prop_desc,
                )


def test_substantial_model_visible_prompts_are_not_duplicated_by_distinct_definitions():
    """Compatibility aliases may point to one string object; separate copies may not."""
    by_text: dict[str, list[tuple[str, int]]] = defaultdict(list)
    checked = 0
    for label, text in _iter_explicit_model_visible_prompt_texts():
        normalized = _normalize_prompt_for_duplicate_check(text)
        if len(normalized) < 160:
            continue
        checked += 1
        by_text[normalized].append((label, id(text)))

    failures: list[str] = []
    for normalized, entries in by_text.items():
        if len(entries) <= 1:
            continue
        object_ids = {object_id for _label, object_id in entries}
        if len(object_ids) == 1:
            continue
        labels = ", ".join(label for label, _object_id in entries)
        failures.append(f"{labels} duplicate {len(normalized)} chars")

    assert checked
    assert failures == []


def test_substantial_inline_prompts_are_not_duplicated():
    by_text: dict[str, list[tuple[str, int]]] = defaultdict(list)
    checked = 0
    for rel, path in _discover_prompt_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _iter_prompt_strings(tree):
            normalized = _normalize_prompt_for_duplicate_check(text)
            if len(normalized) < 160:
                continue
            checked += 1
            by_text[normalized].append((rel, line))

    failures = [
        ", ".join(f"{rel}:{line}" for rel, line in entries)
        for entries in by_text.values()
        if len(entries) > 1
    ]

    assert checked
    assert failures == []


def test_persona_files_are_bridge_agnostic_utf8_clean_and_english_first():
    persona_dir = Path(__file__).resolve().parents[2] / "personas"
    checked = 0
    failures: list[str] = []
    for path in sorted(persona_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        checked += 1
        for issue in _scan_text(text):
            failures.append(f"{path.name} {issue}")
        if not _has_cjk(text):
            failures.append(f"{path.name} is missing concise Chinese operator summary")
        if not _dominant_english_before_chinese_summary(text):
            failures.append(f"{path.name} is not English-first with concise Chinese summary")
        if path.name == "environment.md":
            if re.search(r"\benvironment\s+(tools?|apply|file workflow)\b", text, flags=re.I):
                failures.append(f"{path.name} exposes internal environment workflow wording")
    assert checked
    assert failures == []


# ── Layer 2: AST auto-discovery across the whole app/ tree ─────────
# Phrases that reliably start a model-visible instruction prompt. Matching any
# of these inside a string literal marks that literal as a prompt to audit.
_PROMPT_LAUNCHERS = re.compile(
    r"\bYou are (?:a|an|the|not)\b"
    r"|\bYou decide whether\b"
    r"|\bYou write\b|\bYou create\b|\bYou review\b|\bYou refine\b"
    r"|\bYou deduplicate\b"
    r"|\bReturn strict JSON\b|\bOutput strict JSON\b"
    r"|\bReturn a strict JSON\b"
)

# Files whose long English+launcher strings are NOT model-visible prompts
# (developer docstrings, log templates). Keep this list tiny and justified.
_NON_PROMPT_FILES = {
    "app/tests/test_prompt_hygiene.py",  # this file's own regex literals
}

_APP_PKG = Path(__file__).resolve().parent.parent  # the app/ package directory


def _concat_string(node: ast.AST) -> str:
    """Flatten all string constants under an expression (handles implicit and
    '+' concatenation, f-string literal parts)."""
    parts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
    return "".join(parts)


def _iter_prompt_strings(tree: ast.AST):
    """Yield (lineno, flattened_text) for every string expression that builds a
    prompt, regardless of where it sits in the syntax tree.

    A prompt expression is any string-bearing node (implicit/`+`/f-string
    concatenation, or a dict value) whose flattened content is substantial and
    contains a launcher phrase. Nested nodes are de-duplicated so one prompt is
    reported once at its outermost span.
    """
    # String-producing node types: JoinedStr (f-strings, which subsume their
    # literal parts) and BinOp (explicit '+' concat). Bare Constant strings are
    # included only when not already inside a JoinedStr/BinOp we will report.
    candidates: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Constant)):
            if isinstance(node, ast.Constant) and not isinstance(node.value, str):
                continue
            text = _concat_string(node)
            if len(text) < 120 or not _PROMPT_LAUNCHERS.search(text):
                continue
            candidates.append((getattr(node, "lineno", -1), text, node))

    # De-duplicate: drop any candidate whose text is a substring of a larger
    # candidate (i.e. a Constant chunk inside a JoinedStr already captured).
    candidates.sort(key=lambda c: len(c[1]), reverse=True)
    kept: list[tuple[int, str]] = []
    kept_texts: list[str] = []
    for line, text, _node in candidates:
        if any(text in bigger for bigger in kept_texts):
            continue
        kept_texts.append(text)
        kept.append((line, text))
    seen_lines: set[int] = set()
    for line, text in sorted(kept):
        if line in seen_lines:
            continue
        seen_lines.add(line)
        yield line, text


def _discover_prompt_files():
    for path in sorted(_APP_PKG.rglob("*.py")):
        rel = path.relative_to(_APP_PKG.parent).as_posix()
        if "/__pycache__/" in f"/{rel}":
            continue
        if rel in _NON_PROMPT_FILES:
            continue
        yield rel, path


def test_all_inline_prompts_are_bridge_agnostic_and_utf8_clean():
    """Source-level safety net: every inline prompt in the tree, no manual list."""
    checked = 0
    failures: list[str] = []
    for rel, path in _discover_prompt_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _iter_prompt_strings(tree):
            checked += 1
            for issue in _scan_text(text):
                failures.append(f"{rel}:{line} {issue}")
    assert checked, "AST discovery found no prompts; launcher regex likely broke"
    assert failures == [], "Inline prompt hygiene failures:\n" + "\n".join(failures)


def test_all_inline_prompts_carry_chinese_summary():
    """Every substantial inline prompt must append a concise Chinese summary."""
    checked = 0
    missing: list[str] = []
    for rel, path in _discover_prompt_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _iter_prompt_strings(tree):
            checked += 1
            if not _has_cjk(text):
                missing.append(f"{rel}:{line} (first 60 chars: {text[:60]!r})")
    assert checked
    assert missing == [], (
        "Inline prompts missing a Chinese operator summary:\n" + "\n".join(missing)
    )


def test_framework_first_fanout_guidance_is_visible_to_orchestrator_tools_and_helpers():
    """Large tasks need one shared workflow across the main process, delegate tool, and helpers."""
    from app.core import round_prompts
    from app.llm.tools import helper_prompt_catalog, tool_schemas

    main_prompt = round_prompts.ROUND2_SYSTEM_TEMPLATE
    helper_prompt = helper_prompt_catalog._HELPER_CONSISTENCY_CONTRACT + helper_prompt_catalog._HELPER_SYSTEM_CODE
    delegate_prompt = str(tool_schemas.DELEGATE_TOOL_SCHEMA["function"]["description"])
    delegate_task_prompt = str(
        tool_schemas.DELEGATE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["prompt"]["description"]
    )

    for label, text in {
        "round2": main_prompt,
        "helper": helper_prompt,
        "delegate": delegate_prompt,
        "delegate.tasks.prompt": delegate_task_prompt,
    }.items():
        lowered = text.lower()
        assert "shared framework contract" in lowered, label
        assert "slice" in lowered or "slices" in lowered, label
        assert "segment" in lowered or "segments" in lowered, label
        assert _has_cjk(text), label


def test_code_helper_compile_diagnostics_are_visible_and_normalized():
    """Technical helper troubleshooting guidance should be model-visible and English-first."""
    from app.llm.tools import helper_prompt_catalog

    prompt = helper_prompt_catalog._select_helper_system("code", "easy")

    assert "Compile and Runtime Diagnostics" in prompt
    assert "Include paths" in prompt
    assert "Linking and entry points" in prompt
    assert "Runtime and encoding" in prompt
    assert "常见编译/运行时错误速查" not in prompt
    assert "优先使用工具提示、平台事实和最小修复" in prompt


def test_helper_request_envelope_guidance_is_visible():
    """The main process and delegate schema should request structured helper envelopes."""
    from app.core import round_prompts
    from app.llm.tools import tool_schemas

    main_prompt = round_prompts.ROUND2_SYSTEM_TEMPLATE
    task_props = tool_schemas.DELEGATE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
    delegate_task_prompt = str(task_props["prompt"]["description"])
    input_files_desc = str(task_props["input_files"]["description"])
    acceptance_desc = str(task_props["acceptance_checks"]["description"])

    for label, text in {
        "round2": main_prompt,
        "delegate.tasks.prompt": delegate_task_prompt,
        "delegate.tasks.input_files": input_files_desc,
        "delegate.tasks.acceptance_checks": acceptance_desc,
    }.items():
        lowered = text.lower()
        assert "helper request envelope" in lowered or "传给 helper" in text or "验收" in text, label
        assert _has_cjk(text), label

    assert "kind='code'" in main_prompt
    assert "project scaffold" in delegate_task_prompt.lower()
    assert "acceptance_checks" in main_prompt


# ── Model-visible style policy ─────────────────────────────────────────────
STYLE_PROMPT_MODULES = [
    "app.core.environment_prompt",
    "app.core.round_prompts",
    "app.core.orchestrator_prompts",
    "app.core.guard_prompts",
    "app.llm.tools.helper_prompt_catalog",
    "app.llm.tools.runtime_hints",
]

STYLE_NEGATIVE_PATTERNS = (
    re.compile(r"\bdo\s+not\b", re.I),
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\bavoid\b", re.I),
    re.compile(r"\bmust\s+not\b", re.I),
    re.compile(r"\bshould\s+not\b", re.I),
    re.compile(r"不要|禁止|不得|不能"),
)

STYLE_EXAMPLE_PATTERNS = (
    re.compile(r"\bexample\b", re.I),
    re.compile(r"例如|反例|典型用法|常见反模式"),
)

# Necessary boundary terms are allowed when they protect safety, file-system
# integrity, or exact tool semantics. Keep this list narrow and phrase-based so
# future prompt additions still prefer positive workflow guidance.
STYLE_ALLOWED_NEGATIVE_CONTEXTS = (
    "not as task completion",
    "not as permission",
    "not a failure",
    "not placeholders",
    "not progress",
    "not a full project mirror",
    "not every file",
    "not the project directory",
    "not chat attachments",
    "not as final",
    "not as edit targets",
    "not direct",
    "not exhaustive",
    "without",
    "不代表",
    "不是",
    "不等于",
    "不暴露",
    "不假装",
    "不改",
    "不写",
    "不直接",
    "不完整",
)

STYLE_ALLOWED_EXAMPLE_CONTEXTS = (
    "exact output matrix",
    "representative tests",
    "line count",
    "source-material",
    "readme",
    "docx",
    "pytest",
    "compile",
    "schema",
    "fixture",
    "示例",
)


def _iter_style_prompt_values(module: ModuleType):
    for name, value in vars(module).items():
        if isinstance(value, str) and _is_prompt_constant(name, value):
            yield name, value


def _dominant_english_before_chinese_summary(text: str) -> bool:
    if len(text) < 120:
        return True
    cjk_positions = [i for i, ch in enumerate(text) if "\u4e00" <= ch <= "\u9fff"]
    if not cjk_positions:
        return False
    ascii_letters = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    cjk_chars = len(cjk_positions)
    # Long prompts may append short Chinese summaries after each core section
    # rather than only at the very end. The invariant is English-dominant source
    # text plus concise Chinese operator summaries, not "all Chinese appears
    # after 90% of the prompt".
    return ascii_letters >= max(cjk_chars * 1.5, 80)


def _allowed_style_context(text: str, match: re.Match[str], allowed: tuple[str, ...]) -> bool:
    start = max(0, match.start() - 140)
    end = min(len(text), match.end() + 140)
    ctx = text[start:end].lower()
    return any(term.lower() in ctx for term in allowed)


def test_core_prompts_keep_english_first_style_and_limited_negative_wording():
    checked = 0
    failures: list[str] = []
    for module_name in STYLE_PROMPT_MODULES:
        module = importlib.import_module(module_name)
        for name, text in _iter_style_prompt_values(module):
            checked += 1
            if not _dominant_english_before_chinese_summary(text):
                failures.append(f"{module_name}.{name} is not English-first with concise Chinese summary")

            neg_hits = []
            for pattern in STYLE_NEGATIVE_PATTERNS:
                for match in pattern.finditer(text):
                    if not _allowed_style_context(text, match, STYLE_ALLOWED_NEGATIVE_CONTEXTS):
                        neg_hits.append(match.group(0))
            if len(neg_hits) > 8:
                failures.append(
                    f"{module_name}.{name} has too many unstructured negative hints: {neg_hits[:8]!r}"
                )

            example_hits = []
            for pattern in STYLE_EXAMPLE_PATTERNS:
                for match in pattern.finditer(text):
                    if not _allowed_style_context(text, match, STYLE_ALLOWED_EXAMPLE_CONTEXTS):
                        example_hits.append(match.group(0))
            if len(example_hits) > 3:
                failures.append(
                    f"{module_name}.{name} has too many concrete example markers: {example_hits[:3]!r}"
                )
    assert checked
    assert failures == []
