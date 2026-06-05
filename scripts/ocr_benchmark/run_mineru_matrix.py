# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.llm.tools.ocr_bridge import _analyze_ocr_runaway, _fold_ocr_runaway

ROOT = Path(__file__).resolve().parents[2]
MINERU_DIR = ROOT / "mineru"
MINERU_PY = MINERU_DIR / "py310" / ("python.exe" if sys.platform == "win32" else "bin/python")

SYMBOL_PATTERNS = {
    "int": ["∫", "\\\\int", "int_", "integral"],
    "iint": ["∬", "\\\\iint"],
    "iiint": ["∭", "\\\\iiint"],
    "oint": ["∮", "\\\\oint"],
    "sum": ["∑", "\\\\sum"],
    "lim": ["lim", "\\\\lim"],
    "frac": ["\\\\frac", "/"],
    "matrix": ["matrix", "[[", "\\\\begin"],
    "infty": ["∞", "\\\\infty"],
}


@dataclass(frozen=True)
class MineruConfig:
    name: str
    backend: str
    method: str
    lang: str
    formula: bool
    table: bool
    formula_ch_support: bool
    mask_inline_formula: bool
    image_analysis: bool = True
    lmdeploy_backend: str = "pytorch"
    disable_torch_compile: bool = True
    vl_max_new_tokens: str = ""
    vl_no_repeat_ngram_size: str = ""
    vl_temperature: str = ""
    vl_top_p: str = ""
    vl_top_k: str = ""
    vl_frequency_penalty: str = ""
    vl_presence_penalty: str = ""
    vl_prompt_default: str = ""
    hybrid_batch_ratio: str = ""
    processing_window: str = ""
    max_concurrent: str = ""

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["MINERU_MODEL_SOURCE"] = "local"
        env["MINERU_TOOLS_CONFIG_JSON"] = str(MINERU_DIR / "mineru.local.json")
        env["HF_HOME"] = str(MINERU_DIR / "hf")
        env["HUGGINGFACE_HUB_CACHE"] = str(MINERU_DIR / "hf")
        env["MINERU_DEVICE_MODE"] = env.get("MINERU_DEVICE_MODE", "cuda")
        env["MINERU_FORMULA_CH_SUPPORT"] = "true" if self.formula_ch_support else "false"
        env["MINERU_OCR_DET_MASK_INLINE_FORMULA_ENABLE"] = "true" if self.mask_inline_formula else "false"
        if self.disable_torch_compile:
            env.setdefault("TORCH_COMPILE_DISABLE", "1")
            env.setdefault("TORCHDYNAMO_DISABLE", "1")
            env.setdefault("ACCELERATE_DYNAMO_BACKEND", "NO")
        if self.vl_max_new_tokens:
            env["MINERU_VL_MAX_NEW_TOKENS"] = self.vl_max_new_tokens
        if self.vl_no_repeat_ngram_size:
            env["MINERU_VL_NO_REPEAT_NGRAM_SIZE"] = self.vl_no_repeat_ngram_size
        if self.vl_temperature:
            env["MINERU_VL_TEMPERATURE"] = self.vl_temperature
        if self.vl_top_p:
            env["MINERU_VL_TOP_P"] = self.vl_top_p
        if self.vl_top_k:
            env["MINERU_VL_TOP_K"] = self.vl_top_k
        if self.vl_frequency_penalty:
            env["MINERU_VL_FREQUENCY_PENALTY"] = self.vl_frequency_penalty
        if self.vl_presence_penalty:
            env["MINERU_VL_PRESENCE_PENALTY"] = self.vl_presence_penalty
        if self.vl_prompt_default:
            env["MINERU_VL_PROMPT_DEFAULT"] = self.vl_prompt_default
        if self.hybrid_batch_ratio:
            env["MINERU_HYBRID_BATCH_RATIO"] = self.hybrid_batch_ratio
        if self.processing_window:
            env["MINERU_PROCESSING_WINDOW_SIZE"] = self.processing_window
        if self.max_concurrent:
            env["MINERU_API_MAX_CONCURRENT_REQUESTS"] = self.max_concurrent
        if sys.platform == "win32":
            env["lmdeploy_backend"] = self.lmdeploy_backend
            env.setdefault("MINERU_LMDEPLOY_DEVICE", "cuda")
            env["MINERU_LMDEPLOY_BACKEND"] = self.lmdeploy_backend
        return env


def default_matrix() -> list[MineruConfig]:
    configs: list[MineruConfig] = []
    for formula_model_name, formula_ch in [("unimernet", False), ("ppformulanet", True)]:
        for backend in ["pipeline", "hybrid-auto-engine", "vlm-auto-engine"]:
            for method in ["ocr", "auto"]:
                configs.append(MineruConfig(
                    name=f"{backend}__{method}__formula_on__{formula_model_name}__mask_on",
                    backend=backend,
                    method=method,
                    lang="ch",
                    formula=True,
                    table=True,
                    formula_ch_support=formula_ch,
                    mask_inline_formula=True,
                ))
        configs.append(MineruConfig(
            name=f"pipeline__ocr__formula_on__{formula_model_name}__mask_off",
            backend="pipeline",
            method="ocr",
            lang="ch",
            formula=True,
            table=True,
            formula_ch_support=formula_ch,
            mask_inline_formula=False,
        ))
    configs.extend([
        MineruConfig(
            name="selected_hybrid__ocr__formula_off__ch",
            backend="hybrid-auto-engine",
            method="ocr",
            lang="ch",
            formula=False,
            table=True,
            formula_ch_support=False,
            mask_inline_formula=True,
        ),
        MineruConfig(
            name="selected_hybrid__auto__formula_off__ch",
            backend="hybrid-auto-engine",
            method="auto",
            lang="ch",
            formula=False,
            table=True,
            formula_ch_support=False,
            mask_inline_formula=True,
        ),
    ])
    configs.append(MineruConfig(
        name="baseline_pipeline__auto__formula_off__ch",
        backend="pipeline",
        method="auto",
        lang="ch",
        formula=False,
        table=True,
        formula_ch_support=False,
        mask_inline_formula=True,
    ))
    return configs


def load_manifest(path: Path, limit: int) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
            if limit and len(rows) >= limit:
                break
    return rows


def run_mineru(image: Path, cfg: MineruConfig, timeout: int, api_url: str | None) -> tuple[bool, str, str, float]:
    with tempfile.TemporaryDirectory(prefix="mineru_bench_") as td:
        out_dir = Path(td) / "out"
        cmd = [
            str(MINERU_PY), "-m", "mineru.cli.client",
            "-p", str(image),
            "-o", str(out_dir),
            "-b", cfg.backend,
            "-m", cfg.method,
            "-l", cfg.lang,
            "-f", str(cfg.formula).lower(),
            "-t", str(cfg.table).lower(),
            "--image-analysis", str(cfg.image_analysis).lower(),
        ]
        if api_url:
            cmd.extend(["--api-url", api_url])
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                env=cfg.env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired as e:
            return False, "", f"timeout after {timeout}s", timeout
        elapsed = time.perf_counter() - start
        text = collect_text(out_dir, image.stem)
        if proc.returncode != 0:
            return False, text, ((proc.stderr or "")[-1200:] + (proc.stdout or "")[-400:]).strip(), elapsed
        return True, text, "", elapsed


def collect_text(out_dir: Path, stem: str) -> str:
    candidates = sorted(out_dir.rglob(f"{stem}.md")) or sorted(out_dir.rglob("*.md"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            return re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
    return ""


def classify_failure(ok: bool, text: str, err: str, elapsed: float) -> str:
    if ok and text.strip() and elapsed < 0.2:
        return "fallback_like_instant_output"
    if ok and not text.strip():
        return "no_markdown_text"
    if ok:
        return ""
    lower = err.lower()
    if "timeout" in lower:
        return "timeout"
    if "result_url" in lower or "/tasks/" in lower:
        return "api_task_without_result"
    if text.strip():
        return "nonzero_rc_with_markdown"
    return "nonzero_rc_no_markdown"


def write_text_artifacts(out_dir: Path, cfg: MineruConfig, case_id: str, text: str) -> tuple[str, str]:
    text_dir = out_dir / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    raw_path = text_dir / f"{cfg.name}__{case_id}.raw.md"
    folded_path = text_dir / f"{cfg.name}__{case_id}.folded.md"
    raw_path.write_text(text, encoding="utf-8")
    analysis = _analyze_ocr_runaway(text)
    folded, _ = _fold_ocr_runaway(text, analysis)
    folded_path.write_text(folded, encoding="utf-8")
    return str(raw_path), str(folded_path)


def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def cer(expected: str, actual: str) -> float:
    exp = normalize(expected)
    act = normalize(actual)
    if not exp:
        return 0.0
    return edit_distance(exp, act) / max(1, len(exp))


def formula_tokens(formulas: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for formula in formulas:
        for token in re.findall(r"\\[A-Za-z]+|[A-Za-z]+|[∫∬∭∮∑∞λΩωαπ≤≥→]+|[_^{}()\[\]/=+\-*]", formula):
            if token.strip():
                tokens.add(token.lower())
    return tokens


def formula_recall(formulas: list[str], actual: str) -> float:
    tokens = formula_tokens(formulas)
    if not tokens:
        return 1.0
    hay = actual.lower().replace(" ", "")
    hit = 0
    for token in tokens:
        variants = {token, token.replace("\\", ""), token.replace("\\", "\\\\")}
        if any(v and v in hay for v in variants):
            hit += 1
    return hit / len(tokens)


def symbol_recall(formulas: list[str], actual: str) -> float:
    expected = []
    joined = "\n".join(formulas).lower()
    for name, patterns in SYMBOL_PATTERNS.items():
        if any(p.lower() in joined for p in patterns):
            expected.append(name)
    if not expected:
        return 1.0
    hay = actual.lower()
    hit = sum(1 for name in expected if any(p.lower() in hay for p in SYMBOL_PATTERNS[name]))
    return hit / len(expected)


def damage_count(text: str) -> int:
    return (
        len(re.findall(r"[(]\s*[)]|[(]\s*[）]|[（]\s*[)]", text))
        + len(re.findall(r"=\s*[\n，,。；;]|=\s*[(]\s*[)]", text))
        + text.count("...")
        + text.count("…")
    )


def score_row(case: dict, ok: bool, text: str, err: str, elapsed: float, cfg: MineruConfig, out_dir: Path) -> dict:
    text_cer = cer(case.get("text", ""), text) if ok else 1.0
    f_recall = formula_recall(case.get("formulas", []), text) if ok else 0.0
    s_recall = symbol_recall(case.get("formulas", []), text) if ok else 0.0
    damage = damage_count(text) if ok else 999
    runaway = _analyze_ocr_runaway(text)
    raw_path, folded_path = write_text_artifacts(out_dir, cfg, case["id"], text)
    score = (1 - min(text_cer, 1.0)) * 0.25 + f_recall * 0.45 + s_recall * 0.25 - min(damage, 20) * 0.01 - min(runaway.get("runaway_score", 0), 2000) / 2000 * 0.5
    return {
        "config": cfg.name,
        "backend": cfg.backend,
        "method": cfg.method,
        "lang": cfg.lang,
        "formula": cfg.formula,
        "formula_model": "pp_formulanet_plus_m" if cfg.formula_ch_support else "unimernet_small",
        "mask_inline_formula": cfg.mask_inline_formula,
        "case_id": case["id"],
        "ok": ok,
        "elapsed_sec": round(elapsed, 3),
        "cer": round(text_cer, 4),
        "formula_recall": round(f_recall, 4),
        "symbol_recall": round(s_recall, 4),
        "damage_count": damage,
        "score": round(score, 4),
        "text_len": len(text),
        "error": err[:1000],
        "failure_kind": classify_failure(ok, text, err, elapsed),
        "runaway_score": runaway.get("runaway_score", 0),
        "runaway_flags": runaway.get("flags", []),
        "raw_text_path": raw_path,
        "folded_text_path": folded_path,
        "lmdeploy_backend": cfg.lmdeploy_backend,
        "disable_torch_compile": cfg.disable_torch_compile,
        "vl_max_new_tokens": cfg.vl_max_new_tokens,
        "vl_no_repeat_ngram_size": cfg.vl_no_repeat_ngram_size,
        "vl_temperature": cfg.vl_temperature,
        "vl_top_p": cfg.vl_top_p,
        "vl_top_k": cfg.vl_top_k,
        "vl_frequency_penalty": cfg.vl_frequency_penalty,
        "vl_presence_penalty": cfg.vl_presence_penalty,
        "vl_prompt_default": cfg.vl_prompt_default,
        "hybrid_batch_ratio": cfg.hybrid_batch_ratio,
        "processing_window": cfg.processing_window,
        "max_concurrent": cfg.max_concurrent,
        "text_preview": text[:500],
    }


def summarize(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["config"], []).append(row)
    summary = []
    for name, items in grouped.items():
        elapsed = [r["elapsed_sec"] for r in items if r["ok"]]
        summary.append({
            "config": name,
            "ok_rate": round(sum(1 for r in items if r["ok"]) / len(items), 4),
            "mean_score": round(statistics.mean(r["score"] for r in items), 4),
            "mean_cer": round(statistics.mean(r["cer"] for r in items), 4),
            "mean_formula_recall": round(statistics.mean(r["formula_recall"] for r in items), 4),
            "mean_symbol_recall": round(statistics.mean(r["symbol_recall"] for r in items), 4),
            "p95_elapsed_sec": round(statistics.quantiles(elapsed, n=20)[-1], 3) if len(elapsed) >= 2 else (elapsed[0] if elapsed else 999),
            "mean_damage_count": round(statistics.mean(r["damage_count"] for r in items), 2),
        })
    summary.sort(key=lambda r: (r["ok_rate"], r["mean_score"], -r["p95_elapsed_sec"]), reverse=True)
    return summary


def selected_configs(names: list[str] | None) -> list[MineruConfig]:
    configs = default_matrix()
    if not names:
        return configs
    wanted = set(names)
    return [cfg for cfg in configs if cfg.name in wanted or cfg.backend in wanted]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="data/ocr_benchmark/manifest.jsonl")
    parser.add_argument("--out", default="data/ocr_benchmark/results")
    parser.add_argument("--limit-cases", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--api-url", default=os.environ.get("MINERU_API_URL", ""))
    parser.add_argument("--config", action="append", help="config name or backend to run; can be repeated")
    args = parser.parse_args()

    manifest = Path(args.manifest)
    cases = load_manifest(manifest, args.limit_cases)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "benchmark_results.jsonl"
    summary_path = out_dir / "summary.json"
    configs = selected_configs(args.config)
    rows: list[dict] = []

    with result_path.open("w", encoding="utf-8") as f:
        for cfg in configs:
            print(f"=== {cfg.name} ===", flush=True)
            for case in cases:
                image = Path(case["image"])
                if not image.is_absolute():
                    image = ROOT / image
                ok, text, err, elapsed = run_mineru(image, cfg, args.timeout, args.api_url.strip() or None)
                row = score_row(case, ok, text, err, elapsed, cfg, out_dir)
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(f"{case['id']}: ok={ok} score={row['score']} formula={row['formula_recall']} time={row['elapsed_sec']}s", flush=True)

    summary = summarize(rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nTop configs:")
    for item in summary[:8]:
        print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {result_path} and {summary_path}")


if __name__ == "__main__":
    main()
