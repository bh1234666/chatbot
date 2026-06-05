"""Helper skill text and skill listing.

Skill bodies are model-visible only when a helper explicitly calls
`read_skill(name=...)`. Keep the main body in English and add a short Chinese
summary after each skill so prompts stay consistent with the rest of the agent.
"""

# Detailed skill bodies are loaded on demand, not injected into every system
# prompt. Each entry gives reusable operating guidance rather than incident-
# specific patches.
BUNDLED_SKILLS: dict = {
    "workspace-deep-dive": """\
# Workspace Deep Dive

## Sandbox Layout
- `.` is the helper sandbox root and current working directory.
- `_shared/` is read-only shared scaffold supplied by the main process.
- `_helpers_shared/` is the writable shared area for sibling helpers.
- `shared_files/` or legacy upload folders contain snapshots of user-uploaded/shared files; paths may include upload-time prefixes.

## Shared Includes And Contracts
When using headers or contracts under `_shared/`, include them with the visible relative path:
- Good: `#include "_shared/common.h"`
- Risky: `#include "common.h"` unless the build command explicitly provides the include directory.

Read `_shared/` contracts before choosing public symbols, CSV schema, benchmark loops, file names, or ownership rules. If several helpers feed the same harness, match the shared interface rather than inventing a local variant.

## Writable Shared Work
Use `_helpers_shared/` for reusable code, headers, build scripts, schemas, or notes that sibling helpers should consume later. Single-helper outputs can stay at the sandbox root. Final user-facing files should also be clearly declared in the final report so the system can copy them back.

## Main Workspace Files
If the main prompt references an existing main-workspace input, use `fetch_to_temp(source='main', paths=[...])`. Successful fetches appear in the sandbox and should be used by local relative name. The sandbox boundary is the working root; parent paths and absolute project paths belong to the main process.

## Environment Project Files
In project/environment mode, helpers work on sparse `_env/...` staged copies. `_env/...` is not a full project mirror. If a required dependency is absent, report the exact project-relative path so the main process can fetch it and resume you.

## Deliverable Reporting
Produced files are copied back by the system from declared outputs. In the final report, list concrete user-visible files in the required JSON file block and keep internal scratch files out unless the main process asked for them.

工作区规则：helper 在沙箱内工作，主区文件先 fetch，到项目模式时只处理 `_env/...` 副本；共享接口用 `_shared/`，可复用产物写 `_helpers_shared/`。""",

    "find-missing-file": """\
# Missing File Recovery

Use this sequence whenever a tool reports file not found, missing header, missing input data, or an unreadable path.

## 1. Read Tool Suggestions
Many file tools return `_suggestions` or a fix hint. If a suggested path clearly matches the intended file, use it directly and continue.

## 2. Locate By Stable Substrings
If suggestions do not resolve it, use the workspace locate/search tools with a stable substring rather than guessing prefixes. Prefixes may differ because of helper names, timestamps, upload snapshots, or copy-back names.

## 3. Header Or Build Inputs
For build errors, inspect the build command and the actual workspace tree. A shared header may need an `_shared/...` include path. A linker error may mean the command omitted the source file that defines the symbol.

## 4. Fetch Main Inputs
If the file is an existing user upload, previous output, or main-workspace input mentioned by the main thread, fetch it into the helper sandbox and use the fetched local path. Use concrete fetched paths rather than parent paths, absolute paths, or guessed `_helpers_shared/...` paths.

## 5. Ask The Main Process
If the needed file is not available to this sandbox, report the exact missing path, why it is needed, and the resume condition. Request the main process to provide it rather than continuing with guessed data.

文件缺失处理：先看工具建议，再 locate，必要时 fetch_to_temp；仍缺失就报告精确依赖并等待主线程补资源。""",

    "shared-files-warning": """\
# Uploaded File Path Stability

Uploaded or shared files may appear under snapshot paths such as `shared_files/<timestamp>_<name>` or legacy upload folders. Those snapshot names are evidence for the current workspace, not stable runtime dependencies.

## Stable Use Pattern
- Fetch or copy the needed content into the sandbox.
- Rename or write a stable local input name such as `data.csv`, `testdata.bin`, or `fixtures/input.txt`.
- Generated source code, scripts, and build files should refer to stable local names, not upload-time snapshot paths.

## Reports
When reporting evidence, mention the original uploaded file if relevant, but keep generated code and reusable scripts independent of session-specific prefixes.

上传文件路径可能带时间戳；代码和脚本应使用稳定本地文件名，不依赖会话快照路径。""",

    "compile-errors": """\
# Compile And Link Error Recovery

Use build output as evidence. Fix the cause that the compiler or linker reports, then rebuild.

## Undefined Reference
Likely causes:
- The defining source file was not linked.
- The symbol name or signature differs from the declaration.
- A required library is missing or ordered incorrectly.

Check declarations, definitions, and the actual build command before editing implementation logic.

## Missing Header
Inspect the workspace tree and any tool fix hint. Shared headers often live under `_shared/`; include them with the visible relative path or adjust the build command deliberately.

## Undeclared Identifier Or Conflicting Types
Search for the real declaration and compare signatures. Align headers and implementation rather than adding unrelated stubs.

## Warnings
Treat warnings as useful evidence. Fix unused variables, signed/unsigned comparisons, implicit declarations, narrowing conversions, and uninitialized data when they affect correctness or portability.

## Runtime Failures
For crashes, inspect initialization, bounds, ownership, NUL termination, integer width, and use-after-free patterns. Build small reproducible cases before large experiments.

编译错误按证据修：先查命令、声明、定义和路径，再做聚焦修改并重新验证。""",

    "doc-incremental-build": """\
# Incremental Document Construction

Build Office documents in small reliable steps rather than one huge script.

## Preferred Flow
1. Create the document skeleton with title, sections, and placeholders for known resources.
2. Append sections in bounded chunks.
3. Insert final PNG/chart resources after they exist.
4. Inspect the final file structure and, when needed, read body text or spot-check tables/images.

## Data And Evidence
Use source CSV/JSON/stdout/OCR text as evidence. Keep numbers, labels, units, figure names, and conclusions consistent with those sources. If a required chart or source is missing, request the resource and pause rather than writing a fake final section.

## Academic And Research Documents
For papers, technical research notes, and benchmark reports, keep evidence, interpretation, and speculation separate. Use real citations only when the source details were supplied or verified from available evidence. If sources were not verified, write a transparent source note or suggested-reading section rather than fabricated references.

## Tables
Use tables for compact comparable fields. Split very wide comparisons into smaller tables, and convert paragraph-length cells into prose, bullets, or appendices before final delivery.

## Formula And Formatting
Preserve formulas in the format supported by the Office tool. After generation, inspect or read enough content to catch raw markup that failed to render.

文档构建按骨架、分段、插图、验收逐步做；学术文档只用已验证来源，表格保持可读，缺资源时请求主线程。""",

    "verification-checklist": """\
# Adversarial Verification Checklist

A verify helper is a reviewer, not the original author. Judge the artifact against acceptance points and evidence.

## General Method
- Start with the user's acceptance requirements.
- Inspect the actual produced files or code, not only the author helper's report.
- Run small independent checks when safe and relevant.
- Preserve exact PASS/FAIL/PARTIAL wording in the first line.

## Code And Algorithms
Check edge cases, empty inputs, duplicates, boundary values, invalid inputs, determinism, round-trip properties, and complexity-sensitive sizes. Confirm the tested implementation is actually the one being delivered.

## Data, Charts, And Reports
Compare claims against CSV/JSON/stdout/source notes. Verify labels, units, field names, row semantics, figure references, and whether the chart data exists.

## Documents
Inspect structure, body text, image/table counts, generated filenames, and whether conclusions match available evidence.

## Report
First line must be one of:
`VERDICT: PASS`
`VERDICT: FAIL`
`VERDICT: PARTIAL`

Then list checks, evidence, failures, and the smallest useful next action.

验证 helper 做独立审查：以验收点和实际证据为准，第一行给 PASS/FAIL/PARTIAL。""",

    "algorithm-pitfalls": """\
# Algorithm Implementation Pitfalls

Use this when implementing or reviewing algorithms, data structures, compression, parsing, or benchmarks.

## Correctness
Check:
- Empty input, one item, repeated values, already-sorted and reverse-sorted inputs.
- Boundary indices, off-by-one loops, sentinel handling, and inclusive/exclusive ranges.
- Integer overflow, signed shifts, modulo behavior, floating-point tolerance, and stable comparison rules.
- Ownership, initialization, cleanup, and byte/string termination for C/C++.

## Round-Trip And Invariants
For encoders, serializers, parsers, compression, and transformations, test round-trip equality and malformed inputs. For data structures, verify ordering, size, membership, and mutation invariants after each operation.

## Performance
Estimate complexity before large runs. Use representative probes, cap slow algorithms, and report when results are extrapolated or not directly comparable.

算法任务重点检查边界、溢出、所有权、不变量、往返一致性和复杂度，不盲跑大规模。""",

    "office-recipes": """\
# Office Tool Recipes

Use Office tools for docx/pptx/xlsx containers. Use normal text/file tools for plain `.txt`, `.md`, `.json`, `.yaml`, and `.csv`.

## DOCX
- Build from confirmed evidence: create a skeleton, append bounded sections, and keep each section coherent.
- Valid block types are `heading`, `paragraph`, `list`, `table`, `image`, `equation`, and `page_break`. Use `paragraph` for ordinary prose and `list` for bullet or numbered content.
- Tables need non-empty two-dimensional rows. Remove empty rows before calling the tool.
- Keep Word tables readable: prefer 3-6 meaningful columns, short cells, and several focused tables over one wide prose-heavy table.
- Image blocks need an existing workspace `path`; generate or fetch images first, then embed them.
- For targeted edits, read the document first and use the exact non-negative `block_index`.
- Verify DOCX data claims with `verify_numbers` or `verify_rigor`; use `read` or `inspect_file` for structural acceptance.

## PPTX
- Keep each slide focused.
- Use existing chart/image files rather than recreating chart logic inside the presentation step.
- Inspect slide count and media count.

## XLSX
- Preserve sheet names, headers, units, and formulas.
- For significant calculations, let a code helper produce checked CSV/JSON results, then write the workbook.
- Inspect workbook structure and relevant cell ranges.
- Use `verify_integrity` for cross-sheet consistency and formula cached-value sanity; it is XLSX-only.

## Formulas
Use the supported formula markup and verify rendered output when formulas are central to the task.

Office 工具用于结构化文档容器；DOCX block/action 必须匹配，复杂计算先由 code helper 给出证据，再由 edit helper 写入文档。""",

    "parallel-helpers-coordination": """\
# Parallel Helper Coordination

The main process coordinates helpers. Helpers do focused work and report evidence; they do not manage sibling lifecycles.

## Fan-Out
Use when subtasks are independent: separate algorithms, files, chapters, charts, or data sources. Each helper should have one clear deliverable type and acceptance target.

## Framework Then Fan-Out
Use when comparable implementations or experiments need one shared harness. First create a framework/spec helper that defines interfaces, build commands, input sizes, distributions, seeds, repetitions, metrics, and CSV schema. Then spawn peer helpers against the shared contract.

## Implementation Then Verification
Use for risky artifacts. Implementation helper produces the artifact; verify helper checks it independently. If verification fails, resume or respawn the original base kind with the failure evidence.

## Resource Freeze
When a helper lacks a chart, OCR evidence, data file, audio text, or other dependency, it should call `request_resource` and freeze. The main process decides whether existing resources satisfy it, spawns a resource helper, refuses the resource, or terminates the blocked helper.

## Retry And Hard Mode
Difficulty belongs in `mode`. Keep the same base kind and use `mode='hard'` for difficult retries, or spawn a fresh task with the observed root cause. New work uses the supported base kinds rather than legacy final-style kinds.

## Coordination Rules
- Weak dependencies may start together if a consumer can freeze cleanly.
- Strong compile/interface dependencies should finish before consumers.
- Avoid multiple helpers writing the same final file unless the main process explicitly chooses a race/backstop pattern.
- The main process performs fan-in, acceptance, and user-facing synthesis.

并行协作由主线程管控；独立任务 fan-out，共享框架先建契约，缺资源时 helper 冻结请求，重试用同类 kind + hard mode。""",
}


def get_skill(name: str) -> str | None:
    """Return skill content by name, or None when absent."""
    return BUNDLED_SKILLS.get(name)


def list_skills() -> list[str]:
    """List available skill names without loading their bodies."""
    return [name for name in BUNDLED_SKILLS.keys() if name != "group-files-warning"]


_SKILL_DESCRIPTIONS: dict[str, str] = {
    "workspace-deep-dive": "Workspace sandbox, _shared/_helpers_shared, main fetches, and environment _env staging.",
    "find-missing-file": "Stepwise missing-file recovery: suggestions, locate, fetch, or request the main process.",
    "shared-files-warning": "Use stable local names instead of timestamped upload snapshot paths in generated code.",
    "compile-errors": "Compiler/linker/runtime error recovery from build evidence.",
    "doc-incremental-build": "Incremental docx/pptx/xlsx construction and evidence-based document closure.",
    "verification-checklist": "Read-only adversarial PASS/FAIL/PARTIAL verification workflow.",
    "algorithm-pitfalls": "Algorithm correctness, round-trip, invariant, and performance pitfalls.",
    "office-recipes": "Office container recipes for DOCX, PPTX, XLSX, formulas, and inspection.",
    "parallel-helpers-coordination": "Fan-out, framework-first, implementation+verification, resource freeze, and hard retries.",
}


def _build_skills_listing() -> str:
    """Build the helper-visible on-demand skill listing."""
    lines = [
        "",
        "## On-Demand Skills",
        "The following skills contain detailed guidance. Load one with `read_skill(name=...)` only when the current task or failure pattern matches it:",
    ]
    for name in list_skills():
        desc = _SKILL_DESCRIPTIONS.get(name, "(no description)")
        lines.append(f"- `{name}`: {desc}")
    lines.append(
        "If the task is already clear, proceed directly. Load a skill when it will change the next action or avoid repeated failure."
    )
    lines.append("按需读取 skill；任务清楚时直接工作，出错或匹配场景时再加载详细指引。")
    return "\n".join(lines) + "\n"


# Backward-compatible alias for older stuck-detector recommendations and saved
# helper transcripts. The alias is intentionally omitted from list_skills() and
# _build_skills_listing() so new model-visible prompts use generic wording.
BUNDLED_SKILLS["group-files-warning"] = BUNDLED_SKILLS["shared-files-warning"]
