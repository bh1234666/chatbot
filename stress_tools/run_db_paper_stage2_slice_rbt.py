"""DB paper stage 2: one-slice short test.

Reuses the framework_contract.md produced by stage 1 and asks the
orchestrator to complete only ``slice_rbt`` (red-black tree). A
wall-clock cap keeps the run bounded while we observe whether the
slice helper produces declared outputs (source, benchmark stub, analysis
markdown) early instead of reading the framework forever.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
WALL_CAP_SEC = 720  # 12 minutes


def find_latest_stage1_contract() -> Path:
    candidates = sorted(
        RUN_DIR.glob("db_paper_stage1_framework_*"),
        reverse=True,
    )
    for cand in candidates:
        contract = cand / "db_index_paper_framework" / "framework_contract.md"
        if contract.is_file():
            return contract
    raise SystemExit("no stage1 framework_contract.md found")


def main() -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"db_paper_stage2_slice_rbt_{run_id}"
    work_dir = session_dir / "db_index_paper_slice"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    contract_src = find_latest_stage1_contract()
    contract_dst = work_dir / "framework_contract.md"
    shutil.copyfile(contract_src, contract_dst)
    print(f"[stage2] reused framework contract: {contract_src} -> {contract_dst}")

    sse_log = session_dir / "sse.jsonl"
    summary = session_dir / "summary.json"

    body = {
        "user_id": f"db-stage2-{run_id}",
        "user_name": "LongTest",
        "message": (
            "Stage 2: framework_contract.md is already present in the working "
            "directory. Complete ONLY slice_rbt (Red-Black Tree). Read just the "
            "slice_rbt section and the per-slice acceptance checklist; do not "
            "re-read the entire contract. Produce the artifact paths declared by "
            "that slice in framework_contract.md (for example, current contracts "
            "may declare slice_rb_tree/rb_tree.c, slice_rb_tree/rb_tree.h, "
            "slice_rb_tree/benchmark_rb.csv, and slice_rb_tree/analysis_rb.md). "
            "This is a bounded smoke run, not a full performance study: keep the "
            "benchmark small enough to finish within this chat turn, for example "
            "1K/10K/100K tiers or sampled 1M data if cheap. If the framework's "
            "full 1M acceptance benchmark is not run, state that fact explicitly "
            "in analysis_rb.md as remaining full-scale verification, but still "
            "produce the CSV, code, header, and analysis with the verified smoke "
            "evidence. Create skeleton output files early, then fill and self-check. "
            "Do not start any other slice. "
            "中文概要：仅完成红黑树 slice，先建骨架再填充，不读完整契约，不动其它切片。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"db-stage2-{run_id}",
        "client_msg_id": f"db-stage2-{run_id}",
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.time()
    line_count = 0
    truncated = False
    first_artifact_at: float | None = None
    declared_outputs_seen_at: dict[str, float] = {}

    artifact_markers = [
        "slice_rb_tree/",
        "rb_tree.c",
        "rb_tree.h",
        "analysis_rb.md",
        "benchmark_rb.csv",
        "src/rbtree",
        "rbtree.c",
        "rbtree.py",
        "analysis/rbt_analysis",
        "rbt_analysis.md",
        "bench_results/rbt",
        "bench/bench_rbt",
    ]

    print(f"[stage2] run={run_id}")
    print(f"[stage2] work_dir={work_dir}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp, sse_log.open(
            "w", encoding="utf-8"
        ) as fh:
            while True:
                elapsed = time.time() - started
                if elapsed > WALL_CAP_SEC:
                    truncated = True
                    print(f"[stage2] wall-cap hit at {elapsed:.1f}s; closing")
                    break
                line = resp.readline()
                if not line:
                    print(f"[stage2] stream ended at {elapsed:.1f}s")
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if not text:
                    continue
                fh.write(
                    json.dumps(
                        {"t": round(time.time() - started, 2), "line": text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                line_count += 1
                for marker in artifact_markers:
                    if marker in text and marker not in declared_outputs_seen_at:
                        declared_outputs_seen_at[marker] = time.time() - started
                        if first_artifact_at is None:
                            first_artifact_at = declared_outputs_seen_at[marker]
                            print(
                                f"[stage2] first artifact reference '{marker}' at {first_artifact_at:.1f}s"
                            )
    except Exception as exc:  # noqa: BLE001
        print(f"[stage2] stream error: {exc!r}")

    # Inspect produced artifacts under work_dir (excluding the contract we copied in)
    produced = [
        str(p.relative_to(work_dir))
        for p in work_dir.rglob("*")
        if p.is_file() and p.name != "framework_contract.md"
    ]
    def _is_rbt_source(rel: str) -> bool:
        low = rel.replace("\\", "/").lower()
        name = Path(low).name
        return name in {"rb_tree.c", "rb_tree.h", "rbtree.c", "rbtree.h", "rbtree.py"} or "rbtree" in name

    def _is_rbt_analysis(rel: str) -> bool:
        low = rel.replace("\\", "/").lower()
        name = Path(low).name
        return name in {"analysis_rb.md", "rbt_analysis.md"} or "rbt_analysis" in low

    rbt_source = [p for p in produced if _is_rbt_source(p)]
    rbt_analysis = [p for p in produced if _is_rbt_analysis(p)]
    bench_csv = [p for p in produced if p.endswith(".csv")]

    summary_data = {
        "run_id": run_id,
        "stage": 2,
        "wall_cap_sec": WALL_CAP_SEC,
        "elapsed_sec": round(time.time() - started, 2),
        "lines": line_count,
        "truncated": truncated,
        "first_artifact_stream_at": first_artifact_at,
        "declared_outputs_seen_at": declared_outputs_seen_at,
        "produced_files": produced,
        "rbt_source_files": rbt_source,
        "rbt_analysis_files": rbt_analysis,
        "bench_csv_files": bench_csv,
        "work_dir": str(work_dir),
        "sse_log": str(sse_log),
    }
    summary.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[stage2] summary written to {summary}")
    print(json.dumps(summary_data, ensure_ascii=False, indent=2))
    return 0 if (rbt_source and rbt_analysis and bench_csv) else 1


if __name__ == "__main__":
    sys.exit(main())
