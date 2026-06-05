from __future__ import annotations

import argparse
import glob
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cache_report import (  # noqa: E402
    evaluate_hash_chain_stability_gate,
    evaluate_hit_rate_gate,
    evaluate_prefix_hash_chain_stability_gate,
    evaluate_shape_coverage_gate,
    evaluate_warm_hit_rate_gate,
    load_cache_gate_baseline,
    load_hash_chain_stability_gate_baseline,
    load_long_helper_prompt_threshold,
    load_prefix_hash_chain_stability_gate_baseline,
    load_reference_hit_rate_baseline,
    load_reference_shape_coverage_baseline,
    load_reference_warm_hit_rate_baseline,
    load_shape_coverage_gate_baseline,
    load_warm_cache_gate_baseline,
    parse_debug_logs,
    render_cache_report_markdown,
)


def _parse_gate(values: list[str]) -> dict[str, float]:
    gates: dict[str, float] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"invalid --min-hit-rate value: {raw!r}; expected tag=percent")
        tag, value = raw.split("=", 1)
        tag = tag.strip()
        if not tag:
            raise SystemExit(f"invalid --min-hit-rate tag: {raw!r}")
        gates[tag] = float(value)
    return gates


def _expand_log_paths(values: list[str]) -> list[str]:
    paths: list[str] = []
    for raw in values:
        if any(ch in raw for ch in "*?["):
            matches = sorted(glob.glob(raw))
            if matches:
                paths.extend(matches)
                continue
        paths.append(raw)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a prompt cache report from debug logs.")
    parser.add_argument("logs", nargs="+", help="Debug log files to parse.")
    parser.add_argument("-o", "--output", default="cache_report.md", help="Markdown output path.")
    parser.add_argument(
        "--min-hit-rate",
        action="append",
        default=[],
        metavar="TAG=RATE",
        help="Optional gate, for example main=85 or helper_kind.read=80.",
    )
    parser.add_argument(
        "--baseline",
        help=(
            "Optional JSON baseline file. `minimum_*` fields are hard gates; "
            "`reference_*` fields are report-only convergence lines."
        ),
    )
    parser.add_argument(
        "--min-shape-coverage",
        action="append",
        default=[],
        metavar="TAG=RATE",
        help="Optional prompt-shape coverage gate, for example main=95 or helper.x|model=90.",
    )
    parser.add_argument(
        "--min-warm-hit-rate",
        action="append",
        default=[],
        metavar="TAG=RATE",
        help="Optional steady-state hit-rate gate after skipping the first same tag/model calls.",
    )
    parser.add_argument(
        "--warm-skip-first",
        type=int,
        default=2,
        help="Number of first same tag/model calls excluded from --min-warm-hit-rate gates.",
    )
    parser.add_argument(
        "--max-unstable-hash-chain",
        action="append",
        default=[],
        metavar="LABEL=COUNT",
        help="Optional hash-chain stability gate, for example tools_loop.iter1.main=0.",
    )
    parser.add_argument(
        "--max-unstable-prefix-hash-chain",
        action="append",
        default=[],
        metavar="LABEL=COUNT",
        help="Optional prefix-only hash-chain gate for system_static/tool_schema segments.",
    )
    args = parser.parse_args(argv)

    report = parse_debug_logs(_expand_log_paths(args.logs))
    long_helper_threshold = load_long_helper_prompt_threshold(args.baseline) if args.baseline else 20000
    reference_hit = (
        load_reference_hit_rate_baseline(args.baseline)
        if args.baseline else {}
    )
    reference_warm = (
        load_reference_warm_hit_rate_baseline(args.baseline)
        if args.baseline else {}
    )
    reference_shape = (
        load_reference_shape_coverage_baseline(args.baseline)
        if args.baseline else {}
    )
    markdown = render_cache_report_markdown(
        report,
        reference_hit_rate_by_tag=reference_hit,
        reference_warm_hit_rate_by_tag=reference_warm,
        reference_shape_coverage_by_tag=reference_shape,
        long_helper_min_prompt_tokens=long_helper_threshold,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    gates = load_cache_gate_baseline(args.baseline) if args.baseline else {}
    gates.update(_parse_gate(args.min_hit_rate))
    coverage_gates = load_shape_coverage_gate_baseline(args.baseline) if args.baseline else {}
    coverage_gates.update(_parse_gate(args.min_shape_coverage))
    hash_chain_gates = load_hash_chain_stability_gate_baseline(args.baseline) if args.baseline else {}
    hash_chain_gates.update(_parse_gate(args.max_unstable_hash_chain))
    prefix_hash_chain_gates = (
        load_prefix_hash_chain_stability_gate_baseline(args.baseline)
        if args.baseline else {}
    )
    prefix_hash_chain_gates.update(_parse_gate(args.max_unstable_prefix_hash_chain))
    failures = []
    failures.extend(evaluate_hit_rate_gate(
        report,
        minimum_by_tag=gates,
        long_helper_min_prompt_tokens=long_helper_threshold,
    ))
    warm_gates = load_warm_cache_gate_baseline(args.baseline) if args.baseline else {}
    warm_gates.update(_parse_gate(args.min_warm_hit_rate))
    failures.extend(evaluate_warm_hit_rate_gate(
        report,
        minimum_by_tag=warm_gates,
        long_helper_min_prompt_tokens=long_helper_threshold,
        skip_first=args.warm_skip_first,
    ))
    failures.extend(evaluate_shape_coverage_gate(
        report,
        minimum_by_tag=coverage_gates,
        long_helper_min_prompt_tokens=long_helper_threshold,
    ))
    failures.extend(evaluate_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label=hash_chain_gates,
    ))
    failures.extend(evaluate_prefix_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label=prefix_hash_chain_gates,
    ))
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}", file=sys.stderr)
        print(f"Wrote {output}", file=sys.stderr)
        return 2
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
