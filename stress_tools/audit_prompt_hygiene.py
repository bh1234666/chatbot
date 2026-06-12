from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_STANDARDS = [
    "English model-facing body first; Chinese operator summary must be concise and secondary.",
    "No bridge/application-specific special cases in prompts; internal helper/tool names are allowed only when registered and reachable.",
    "No unnecessary negative prompting; prefer factual tool/workflow evidence that leaves decisions to the model.",
    "No substantial duplicated prompt blocks across independent definitions.",
    "Prompt descriptions must match current orchestration, helper, tool-schema, cache, and environment workflows.",
]

AUDIT_TRIGGERS = [
    "Run after every prompt, tool-schema, helper-flow, cache-layering, or environment-mode change batch.",
    "Run periodically during long quality-tuning sessions so several small edits cannot silently drift.",
    "Run before treating benchmark regressions as model weakness; first rule out prompt contradiction or stale workflow facts.",
]

MANUAL_REVIEW_POINTS = [
    "Compare workflow claims with the current implementation entry point and the tool result shape they cite.",
    "Verify helper/tool names such as voice, OCR, TTS, image, draw, markdown, edit, and code against current registrations.",
    "For long prompts with historical logs, delete only text that no longer carries a distinct decision fact, quality constraint, or recovery path.",
    "Prefer adding factual runtime hints over hard symbolic routing when model judgment can reasonably handle the decision.",
    "When a prompt names an acceptance path, ensure the path still exists in tests or runtime code rather than relying on old logs.",
]

WORKFLOW_CONTRACT_ANCHORS = [
    "app/core/orchestrator.py",
    "app/core/round_prompts.py",
    "app/core/orchestrator_prompts.py",
    "app/core/environment_prompt.py",
    "app/llm/tools/delegate_runner.py",
    "app/llm/tools/helper_prompt_catalog.py",
    "app/llm/tools/helper_prompts.py",
    "app/llm/tools/tool_schemas.py",
    "app/llm/tools/registry.py",
    "app/llm/tools/runtime_hints.py",
    "tests/test_model_visible_prompt_contract.py",
    "tests/test_orchestrator_prompts.py",
    "tests/test_environment_mode.py",
    "tests/test_tool_schemas.py",
    "tests/test_prompt_cache_layering.py",
]

_PROMPT_NAME_KEYWORDS = (
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

_PROMPT_LAUNCHERS = re.compile(
    r"\bYou are (?:a|an|the|not)\b"
    r"|\bYou decide whether\b"
    r"|\bYou write\b|\bYou create\b|\bYou review\b|\bYou refine\b"
    r"|\bReturn strict JSON\b|\bOutput strict JSON\b"
    r"|\bReturn a strict JSON\b"
)

_NEGATIVE_PATTERNS = (
    re.compile(r"\bdo\s+not\b", re.I),
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\bavoid\b", re.I),
    re.compile(r"\bmust\s+not\b", re.I),
    re.compile(r"\bshould\s+not\b", re.I),
    re.compile(r"不要|禁止|不得|不能"),
)

_CONCRETE_TASK_MARKERS = (
    re.compile(r"\bclawbench\b", re.I),
    re.compile(r"\bt[1-5]-[a-z0-9-]+\b", re.I),
    re.compile(r"\bwhen implementing\b", re.I),
    re.compile(r"在实现.+?时"),
    re.compile(r"论文撰写|两份作业|具体作业|基准题"),
)


@dataclass(frozen=True)
class PromptRecord:
    label: str
    rel_path: str
    line: int
    text: str

QUICK_TESTS = [
    "tests/test_source_encoding.py",
    "app/tests/test_prompt_hygiene.py",
    "tests/test_model_visible_prompt_contract.py",
    "tests/test_orchestrator_prompts.py",
    "tests/test_environment_mode.py::test_environment_prompt_addon_chat_mode_empty",
    "tests/test_environment_mode.py::test_environment_prompts_keep_bulk_source_body_extraction_in_read_helpers",
    "tests/test_environment_mode.py::test_environment_prompts_distinguish_project_paths_from_env_staging",
    "tests/test_environment_mode.py::test_environment_prompts_delegate_long_file_authoring_from_main_thread",
    "tests/test_environment_mode.py::test_environment_round2_prompt_mentions_light_project_helpers",
    "tests/test_tool_schemas.py",
]

FULL_EXTRA_TESTS = [
    "app/tests/test_prompt_stability.py",
    "app/tests/test_framework_guard.py",
    "app/tests/test_toolchain_cache.py",
    "tests/test_prompt_cache_layering.py",
    "tests/test_prompt_cache_observer.py",
    "tests/test_tool_meta.py",
]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _dominant_english_before_chinese_summary(text: str) -> bool:
    if len(text) < 120:
        return True
    cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_letters = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return cjk_count > 0 and ascii_letters >= max(cjk_count * 1.5, 80)


def _concat_string(node: ast.AST | None) -> str:
    if node is None:
        return ""
    parts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
    return "".join(parts)


def _static_prompt_text(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return _concat_string(node)
    return ""


def _looks_like_prompt_name(name: str) -> bool:
    upper = name.upper()
    return name.startswith("ROUND") or any(keyword in upper for keyword in _PROMPT_NAME_KEYWORDS)


def _normalize_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _iter_app_prompt_records() -> list[PromptRecord]:
    records: list[PromptRecord] = []
    app_dir = ROOT / "app"
    if not app_dir.exists():
        return records
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/__pycache__/" in f"/{rel}" or rel.startswith("app/tests/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        module_level_texts: set[str] = set()
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
            if not targets or not any(_looks_like_prompt_name(target) for target in targets):
                continue
            text = _static_prompt_text(value)
            if len(text.strip()) >= 40:
                module_level_texts.add(_normalize_prompt(text))
                records.append(
                    PromptRecord(
                        label=f"{rel}:{','.join(targets)}",
                        rel_path=rel,
                        line=getattr(node, "lineno", 1),
                        text=text,
                    )
                )

        candidates: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Constant)):
                if isinstance(node, ast.Constant) and not isinstance(node.value, str):
                    continue
                text = _concat_string(node)
                if len(text) >= 120 and _PROMPT_LAUNCHERS.search(text):
                    candidates.append((getattr(node, "lineno", 1), text))
        candidates.sort(key=lambda item: len(item[1]), reverse=True)
        kept: list[tuple[int, str]] = []
        kept_texts: list[str] = []
        for line, text in candidates:
            if any(text in bigger for bigger in kept_texts):
                continue
            kept_texts.append(text)
            kept.append((line, text))
        for line, text in kept:
            if _normalize_prompt(text) in module_level_texts:
                continue
            records.append(PromptRecord(label=f"{rel}:{line}", rel_path=rel, line=line, text=text))

    persona_dir = ROOT / "personas"
    if persona_dir.exists():
        for path in sorted(persona_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            records.append(
                PromptRecord(
                    label=f"{path.relative_to(ROOT).as_posix()}:persona",
                    rel_path=path.relative_to(ROOT).as_posix(),
                    line=1,
                    text=text,
                )
            )
    return records


def _negative_count(text: str) -> int:
    return sum(1 for pattern in _NEGATIVE_PATTERNS for _match in pattern.finditer(text))


def _concrete_marker_hits(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in _CONCRETE_TASK_MARKERS:
        if pattern.search(text):
            hits.append(pattern.pattern)
    return hits


def _capability_fact_lines() -> list[str]:
    try:
        from app.llm import voice_output
        from app.llm.tools import delegate, registry
        from app.llm.tools.helper_kinds import HELPER_CONFIGS, MODEL_VISIBLE_HELPER_KINDS

        main_thread_tools = sorted(
            tool["function"]["name"]
            for tool in registry.ROUND2_TOOLS
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        )
        helper_tools = sorted(
            tool["function"]["name"]
            for tool in delegate._HELPER_TOOLS
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        )
        visible_kinds = sorted(MODEL_VISIBLE_HELPER_KINDS)
        configured_kinds = sorted(HELPER_CONFIGS)
        legacy_config_keys = [
            kind for kind in configured_kinds
            if kind not in set(visible_kinds)
        ]
        return [
            f"- Model-visible helper kinds: {', '.join(visible_kinds)}",
            f"- Runtime helper config keys: {', '.join(configured_kinds)}",
            f"- Legacy-only helper config keys: {', '.join(legacy_config_keys) if legacy_config_keys else '(none)'}",
            f"- Main-process Round 2 tools: {', '.join(main_thread_tools)}",
            f"- Helper runtime tools: {', '.join(helper_tools)}",
            f"- Voice/TTS policy functions importable: {callable(voice_output.decide_voice) and callable(voice_output.should_keep_round2_tts_tool)}",
        ]
    except Exception as exc:
        return [f"- Capability registry import failed; inspect registrations manually: {type(exc).__name__}: {exc}"]


def _prompt_inventory_report() -> str:
    records = _iter_app_prompt_records()
    long_records = [record for record in records if len(_normalize_prompt(record.text)) >= 160]
    by_text: dict[str, list[PromptRecord]] = {}
    for record in long_records:
        by_text.setdefault(_normalize_prompt(record.text), []).append(record)

    review_items: list[tuple[int, str]] = []
    for normalized, entries in by_text.items():
        labels = ", ".join(entry.label for entry in entries[:5])
        if len(entries) > 1:
            review_items.append((30, f"Duplicate prompt block ({len(normalized)} chars): {labels}"))

    for record in records:
        text = record.text
        if len(text.strip()) < 80:
            continue
        if not _has_cjk(text):
            review_items.append((10, f"Missing Chinese summary: {record.label}"))
        elif not _dominant_english_before_chinese_summary(text):
            review_items.append((20, f"English-first ratio needs review: {record.label}"))
        markers = _concrete_marker_hits(text)
        if markers:
            review_items.append((25, f"Possible task-specific wording: {record.label} markers={markers[:3]}"))
        neg_count = _negative_count(text)
        if neg_count > 10:
            review_items.append((40, f"Heavy negative wording ({neg_count} hits): {record.label}"))

    review_items.sort(key=lambda item: item[0])
    review_lines = [f"- P{priority}: {message}" for priority, message in review_items[:40]]
    if not review_lines:
        review_lines = ["- No high-priority automatic prompt hygiene findings."]

    duplicate_groups = sum(1 for entries in by_text.values() if len(entries) > 1)
    lines = [
        "## Prompt Inventory",
        f"- Prompt-like records scanned: {len(records)}",
        f"- Long records checked for duplication: {len(long_records)}",
        f"- Exact duplicate groups: {duplicate_groups}",
        "",
        "## Capability Facts From Current Registrations",
        *_capability_fact_lines(),
        "",
        "## High-Priority Review Queue",
        "Sorted by risk: missing summaries, English-first drift, task-specific wording, duplication, then negative-prompting density.",
        *review_lines,
        "",
        "## Workflow Contract Anchors",
        "Review these files/tests when prompt text describes orchestration, helpers, tools, cache, staging, or acceptance flow:",
        *[f"- {path}" for path in WORKFLOW_CONTRACT_ANCHORS],
    ]
    return "\n".join(lines)


def _report_text(*, full: bool, pytest_args: list[str]) -> str:
    tests = list(QUICK_TESTS)
    if full:
        tests.extend(FULL_EXTRA_TESTS)
    audit_cmd = [str(Path(sys.executable)), "stress_tools/audit_prompt_hygiene.py"]
    if full:
        audit_cmd.append("--full")
    audit_cmd.extend(pytest_args or ["-q"])
    lines = [
        "# Periodic Prompt Hygiene Audit",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Run this audit repeatedly while prompt quality is being tuned. It is a drift guard, not a replacement for benchmark runs.",
        "",
        "## When To Run",
        *[f"- {item}" for item in AUDIT_TRIGGERS],
        "",
        "## Standards",
        *[f"- {item}" for item in AUDIT_STANDARDS],
        "",
        "## Test Scope",
        *[f"- {test}" for test in tests],
        "",
        "## Command",
        "```powershell",
        "$env:PYTHONIOENCODING='utf-8'",
        " ".join(audit_cmd),
        "```",
        "",
        "## Manual Review Points",
        *[f"- {item}" for item in MANUAL_REVIEW_POINTS],
        "",
        _prompt_inventory_report(),
        "",
        "## Expected Failure Handling",
        "- Format/style failures usually mean the prompt needs English-first wording or a shorter Chinese summary.",
        "- Capability-registration failures mean the prompt names a helper/tool that may be stale; fix the prompt or the registration after checking code.",
        "- Workflow-contract failures mean the prompt and current process disagree; inspect implementation before weakening the assertion.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the periodic prompt audit: English-first prompts with concise Chinese summaries, "
            "duplicate/special-case drift checks, and current workflow contract tests."
        )
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run prompt cache/stability tests that take longer.",
    )
    parser.add_argument(
        "--show-plan",
        action="store_true",
        help="Print the audit standards, test scope, and manual review checklist before running tests.",
    )
    parser.add_argument(
        "--write-report",
        metavar="PATH",
        help="Write the audit standards and selected test scope to a Markdown report, then run tests.",
    )
    parser.add_argument(
        "--report-dir",
        metavar="DIR",
        help="Write a timestamped Markdown report before each audit run.",
    )
    parser.add_argument(
        "--repeat-minutes",
        type=float,
        default=0.0,
        help="Run the same audit periodically every N minutes. Default 0 runs once.",
    )
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=0,
        help="Maximum number of periodic audit runs. Default 0 means run until interrupted when --repeat-minutes is set.",
    )
    args, pytest_args = parser.parse_known_args()

    tests = list(QUICK_TESTS)
    if args.full:
        tests.extend(FULL_EXTRA_TESTS)
    if args.show_plan:
        print(_report_text(full=args.full, pytest_args=pytest_args))
    def write_report(path_arg: str) -> Path:
        report_path = (ROOT / path_arg).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_report_text(full=args.full, pytest_args=pytest_args), encoding="utf-8")
        print(f"Prompt audit report written: {report_path}")
        return report_path

    def write_timestamped_report() -> Path | None:
        if not args.report_dir:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_dir = (ROOT / args.report_dir).resolve()
        report_dir.mkdir(parents=True, exist_ok=True)
        return write_report(str(report_dir / f"prompt_audit_{stamp}.md"))

    if args.write_report:
        write_report(args.write_report)
    write_timestamped_report()
    cmd = [sys.executable, "-m", "pytest", *tests, *(pytest_args or ["-q"])]
    print("Prompt audit command:", flush=True)
    print(" ".join(cmd), flush=True)

    repeat_seconds = max(0.0, float(args.repeat_minutes or 0.0) * 60.0)
    if repeat_seconds <= 0:
        return subprocess.call(cmd, cwd=ROOT)

    run_index = 0
    last_rc = 0
    try:
        while True:
            run_index += 1
            print(f"\n=== Prompt audit periodic run {run_index} ===", flush=True)
            if run_index > 1:
                write_timestamped_report()
            last_rc = subprocess.call(cmd, cwd=ROOT)
            if last_rc != 0:
                return last_rc
            if args.repeat_count and run_index >= args.repeat_count:
                return last_rc
            print(f"Next prompt audit in {args.repeat_minutes:g} minute(s). Press Ctrl+C to stop.")
            time.sleep(repeat_seconds)
    except KeyboardInterrupt:
        print("\nPrompt audit loop stopped by user.")
        return last_rc


if __name__ == "__main__":
    raise SystemExit(main())
