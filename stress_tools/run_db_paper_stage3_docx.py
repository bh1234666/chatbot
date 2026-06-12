"""DB paper stage 3: DOCX assembly short test.

Pre-stages a working directory with:
- framework_contract.md from stage 1
- analysis/rbt_analysis.md from stage 2 (real content)
- 4 minimal placeholder analyses for skiplist / btree / bplus / hybrid
- 5 tiny benchmark CSVs

Then asks the orchestrator (edit helper) to assemble the final DOCX
``db_index_paper.docx``. Wall-clock cap keeps the test bounded; we are
measuring whether the DOCX assembly stage can produce a non-empty .docx
using existing slice artifacts.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _driver_common import (  # noqa: E402
    DoneCapture,
    ask_followup_about_delivery,
    parse_followup_answer,
    read_sse_with_stall_cap,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
WALL_CAP_SEC = 1500  # 25 min hard deadline; verify-helper finishes ~10–11min in
STALL_CAP_SEC = 90   # if SSE quiet for 90s, treat as wedged
EXPECTED_ARTIFACT = "db_index_paper.docx"
PLACEHOLDER_TOKEN_RE = (
    r"(?i)(?:\bTODO\b|\bTKTK\b|\bINSERT\b|\blorem\s+ipsum\b|\[\s*[.。…]{2,}\s*\]|"
    r"\bplaceholder\b)"
)


PLACEHOLDER_SECTIONS = [
    ("skiplist", "Skip List"),
    ("btree", "B-Tree"),
    ("bplus", "B+ Tree"),
    ("hybrid", "Hybrid Index (Proposed)"),
]


def latest_dir(prefix: str) -> Path:
    dirs = sorted(RUN_DIR.glob(prefix + "*"), reverse=True)
    if not dirs:
        raise SystemExit(f"no run found for prefix {prefix}")
    return dirs[0]


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.is_file():
            return path
    raise SystemExit(
        "missing required input; tried:\n" + "\n".join(f"  - {p}" for p in paths)
    )


def write_placeholder_analysis(out: Path, slug: str, title: str) -> None:
    text = f"""# {title} — Algorithmic Analysis

*Slice: `slice_{slug}` — Part of the DB Index Structures comparison paper*

## 1. Big-O Complexity Table

| Operation  | Average      | Worst       | Note |
|------------|--------------|-------------|------|
| Insert     | O(log n)     | O(log n)    | Balanced search structure |
| Search     | O(log n)     | O(log n)    | Single descent path |
| Delete     | O(log n)     | O(log n)    | Restructuring localized |
| Range scan | O(log n + m) | O(n)        | Returns m matching keys |

## 2. Disk I/O Considerations

This placeholder section summarizes typical disk-I/O behavior for {title}.
Replace with full slice content when the slice helper completes.

## 3. Range Query Mechanics

{title} supports range queries by traversing the structure in sorted order.

## 4. Concurrency Notes

Concurrent access requires fine-grained locking or non-blocking variants.

## 5. DBMS Usage

{title} is used by various database engines for in-memory and on-disk indexes.

## 6. Empirical Validation

See `bench_results/{slug}.csv` for benchmark data.
"""
    out.write_text(text, encoding="utf-8")


def write_placeholder_csv(out: Path, slug: str) -> None:
    rows = ["operation,n,time_ns,memory_bytes"]
    for op in ("insert", "search", "delete", "range"):
        for i, n in enumerate((1000, 10000, 100000)):
            t = (i + 1) * 800
            mem = n * 24
            rows.append(f"{op},{n},{t},{mem}")
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")


def placeholder_token_hits(text: str) -> list[str]:
    """Return template-placeholder hits without matching natural words like insertion."""
    import re

    return [m.group(0) for m in re.finditer(PLACEHOLDER_TOKEN_RE, text)]


def main() -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"db_paper_stage3_docx_{run_id}"
    work_dir = session_dir / "db_index_paper_docx"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "analysis").mkdir(exist_ok=True)
    (work_dir / "bench_results").mkdir(exist_ok=True)

    stage1_dir = latest_dir("db_paper_stage1_framework_")
    stage2_dir = latest_dir("db_paper_stage2_slice_rbt_")

    contract_src = stage1_dir / "db_index_paper_framework" / "framework_contract.md"
    rbt_analysis_src = first_existing(
        stage2_dir / "db_index_paper_slice" / "slice_rb_tree" / "analysis_rb.md",
        stage2_dir / "db_index_paper_slice" / "analysis" / "rbt_analysis.md",
    )
    rbt_benchmark_src = first_existing(
        stage2_dir / "db_index_paper_slice" / "slice_rb_tree" / "benchmark_rb.csv",
        stage2_dir / "db_index_paper_slice" / "bench_results" / "rbt.csv",
    )

    if not contract_src.is_file():
        raise SystemExit(f"missing framework contract: {contract_src}")

    shutil.copyfile(contract_src, work_dir / "framework_contract.md")
    shutil.copyfile(rbt_analysis_src, work_dir / "analysis" / "rbt_analysis.md")
    shutil.copyfile(rbt_benchmark_src, work_dir / "bench_results" / "rbt.csv")
    for slug, title in PLACEHOLDER_SECTIONS:
        write_placeholder_analysis(
            work_dir / "analysis" / f"{slug}_analysis.md", slug, title
        )
        write_placeholder_csv(work_dir / "bench_results" / f"{slug}.csv", slug)

    print(f"[stage3] run={run_id}")
    print(f"[stage3] work_dir={work_dir}")
    print(f"[stage3] pre-staged inputs:")
    for p in sorted(work_dir.rglob("*")):
        if p.is_file():
            print(f"  - {p.relative_to(work_dir)} ({p.stat().st_size}B)")

    sse_log = session_dir / "sse.jsonl"
    summary = session_dir / "summary.json"

    body = {
        "user_id": f"db-stage3-{run_id}",
        "user_name": "LongTest",
        "message": (
            "Stage 3: assemble the final DOCX. Inputs ready in working dir: "
            "framework_contract.md, analysis/{rbt,skiplist,btree,bplus,hybrid}_"
            "analysis.md, and bench_results/*.csv. Use the edit helper to merge "
            "these into db_index_paper.docx following the framework's section "
            "order: Abstract, Introduction, Background, Comparative Analysis "
            "(four sub-sections), Proposed Hybrid Index, Evaluation Plan, "
            "Related Work, Conclusion, References. Pull comparison-table data "
            "from the CSVs. Do not reference internal helper paths or "
            "_helpers_shared in the final document. "
            "中文概要：把已有 5 份 analysis 与 5 份 CSV 装配成 db_index_paper.docx，"
            "结构按 framework 要求；不暴露 helper 内部路径。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"db-stage3-{run_id}",
        "client_msg_id": f"db-stage3-{run_id}",
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.time()
    docx_seen_at: float | None = None
    capture = DoneCapture()
    stream_result = None

    sse_fh = sse_log.open("w", encoding="utf-8")

    def _on_text(text: str, t: float) -> None:
        nonlocal docx_seen_at
        sse_fh.write(json.dumps({"t": t, "line": text}, ensure_ascii=False) + "\n")
        sse_fh.flush()
        if docx_seen_at is None and EXPECTED_ARTIFACT in text:
            docx_seen_at = t
            print(f"[stage3] docx mention in stream at {docx_seen_at:.1f}s")
        capture.feed_line(text)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            stream_result = read_sse_with_stall_cap(
                resp,
                wall_cap_sec=WALL_CAP_SEC,
                stall_cap_sec=STALL_CAP_SEC,
                on_text=_on_text,
            )
        if stream_result.truncated_wall_cap:
            print(f"[stage3] wall-cap hit at {stream_result.elapsed_sec:.1f}s")
        elif stream_result.truncated_stall_cap:
            print(
                f"[stage3] stall-cap hit at {stream_result.elapsed_sec:.1f}s "
                f"(no SSE activity for {STALL_CAP_SEC}s)"
            )
        elif stream_result.stream_error:
            print(f"[stage3] stream error: {stream_result.stream_error}")
        else:
            print(f"[stage3] stream ended at {stream_result.elapsed_sec:.1f}s")
    finally:
        sse_fh.close()

    line_count = stream_result.line_count if stream_result else 0
    truncated = bool(
        stream_result
        and (stream_result.truncated_wall_cap or stream_result.truncated_stall_cap)
    )

    # The LLM's structured delivery declarations: `event: done` files entries
    # PLUS `kind:artifact_ready` mid-stream events. We do NOT filesystem-search.
    declared = capture.llm_declared_paths(EXPECTED_ARTIFACT)
    followup: dict | None = None
    followup_parsed: dict | None = None
    if not declared and capture.archive_id:
        # Stream may have ended without surfacing the docx (path/mode mismatch
        # in the orchestrator's done payload, or stall/wall-cap). Ask the LLM
        # directly: did you deliver? Trust whatever it says.
        print(f"[stage3] no LLM declaration of {EXPECTED_ARTIFACT}; asking LLM follow-up")
        followup = ask_followup_about_delivery(
            API,
            archive_id=capture.archive_id,
            user_id=body["user_id"],
            current_dir=str(work_dir),
            expected=EXPECTED_ARTIFACT,
            wall_cap_sec=240,
        )
        if followup.get("ok"):
            print(f"[stage3] follow-up answer: {followup.get('answer_text','')[:280]}")
            for entry in followup.get("files") or []:
                hay = " ".join(
                    str(entry.get(k) or "").lower()
                    for k in ("name", "rel_path", "local_path", "url")
                )
                if EXPECTED_ARTIFACT.lower() in hay:
                    declared.append(entry)
            # Parse the prose answer per the prompt's contract.
            followup_parsed = parse_followup_answer(
                followup.get("answer_text", ""), EXPECTED_ARTIFACT
            )
            if followup_parsed.get("declared") and not declared:
                declared.append({
                    "name": followup_parsed.get("path") or EXPECTED_ARTIFACT,
                    "rel_path": followup_parsed.get("path"),
                    "source": "followup_answer_text",
                })

    docx_paths: list[str] = []
    for entry in declared:
        path = entry.get("local_path") or entry.get("rel_path") or entry.get("name") or entry.get("path")
        if path:
            docx_paths.append(str(path))
    docx_present = bool(declared)

    summary_data = {
        "run_id": run_id,
        "stage": 3,
        "wall_cap_sec": WALL_CAP_SEC,
        "stall_cap_sec": STALL_CAP_SEC,
        "elapsed_sec": stream_result.elapsed_sec if stream_result else round(time.time() - started, 2),
        "last_activity_at": stream_result.last_activity_at if stream_result else None,
        "lines": line_count,
        "truncated": truncated,
        "truncated_kind": (
            "wall_cap" if stream_result and stream_result.truncated_wall_cap
            else "stall_cap" if stream_result and stream_result.truncated_stall_cap
            else None
        ),
        "docx_stream_seen_at": docx_seen_at,
        "docx_present": docx_present,
        "docx_paths": docx_paths,
        "llm_declared_deliveries": declared,
        "followup": followup,
        "followup_parsed": followup_parsed,
        "archive_id": capture.archive_id,
        "work_dir": str(work_dir),
        "sse_log": str(sse_log),
    }
    summary.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[stage3] summary written to {summary}")
    print(json.dumps({k: summary_data[k] for k in (
        "run_id", "elapsed_sec", "truncated", "docx_present", "docx_paths"
    )}, ensure_ascii=False, indent=2))
    return 0 if docx_present else 1


if __name__ == "__main__":
    sys.exit(main())
