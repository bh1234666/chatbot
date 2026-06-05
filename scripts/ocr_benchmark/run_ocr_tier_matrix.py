# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.llm.tools.ocr_bridge import _analyze_ocr_runaway, ocr_file_tiered


def iter_images(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".pdf"}
    return sorted(p for p in path.iterdir() if p.suffix.lower() in exts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Image/PDF file or directory")
    parser.add_argument("--out", default="output/ocr_speed_benchmark/tier_matrix")
    parser.add_argument("--tier", action="append", choices=["fast", "balanced", "accurate", "auto"], help="Tier to run; repeatable")
    parser.add_argument("--max-tier", default="accurate", choices=["fast", "balanced", "accurate"])
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--no-upgrade", action="store_true")
    args = parser.parse_args()

    source = Path(args.input)
    if not source.is_absolute():
        source = ROOT / source
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    text_dir = out_dir / "texts"
    out_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    tiers = args.tier or ["fast", "balanced", "accurate", "auto"]
    rows = []
    jsonl = out_dir / "results.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for image in iter_images(source):
            for tier in tiers:
                start = time.perf_counter()
                result = ocr_file_tiered(
                    image,
                    tier=tier,
                    allow_upgrade=not args.no_upgrade,
                    max_tier=args.max_tier,
                    timeout=args.timeout,
                )
                elapsed = time.perf_counter() - start
                analysis = _analyze_ocr_runaway(result.text or "")
                text_path = text_dir / f"{image.stem}__{tier}.txt"
                text_path.write_text(result.text or "", encoding="utf-8")
                row = {
                    "file": image.name,
                    "requested_tier": tier,
                    "final_tier": result.tier,
                    "ok": result.ok,
                    "engine": result.engine,
                    "elapsed_sec": round(elapsed, 3),
                    "reported_elapsed_ms": result.elapsed_ms,
                    "text_len": len(result.text or ""),
                    "raw_text_len": result.raw_text_len,
                    "runaway_score": analysis.get("runaway_score", 0),
                    "folded_spans": len(result.folded_spans),
                    "quality_flags": result.quality_flags,
                    "candidates": result.candidates,
                    "raw_text_path": result.raw_text_path,
                    "text_path": str(text_path),
                    "error": result.error[:1000],
                }
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(json.dumps({k: row[k] for k in ("file", "requested_tier", "final_tier", "ok", "elapsed_sec", "text_len", "runaway_score", "folded_spans")}, ensure_ascii=False), flush=True)

    summary = {
        "rows": len(rows),
        "ok": sum(1 for r in rows if r["ok"]),
        "jsonl": str(jsonl),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
