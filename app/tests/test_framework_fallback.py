"""验证确定性框架兜底检测(2026-05-21)。

守卫第④维度是 LLM 软判定, 且可能因部署合并丢失。本检测用纯代码规则兜底:
≥3 个算法 helper 各产 results_*.csv 横向对比、无 infra/共享框架 task → 警告主线程先建框架。
不依赖 LLM、不依赖提示词部署。命中只警告不阻断(误伤风险低)。

复刻检测逻辑(纯 stdlib)。
"""
import re as _re


def _detect_missing_unified_framework(cleaned):
    try:
        _csv_producers = []
        _has_infra = False
        _refs_shared = False
        for c in cleaned:
            _tid = str(c.get("task_id", "")).strip()
            _kind = str(c.get("kind", "")).strip().lower()
            _mode = str(c.get("mode", "")).strip().lower()
            _prompt = str(c.get("prompt", "") or "")
            _outs = c.get("expected_outputs") or []
            if _mode == "hard" and _tid.endswith("_hard"):
                continue
            if c.get("resume"):
                return None
            _tid_l = _tid.lower()
            if any(k in _tid_l for k in ("infra", "framework", "common", "harness", "bench_common", "scaffold", "spec")):
                _has_infra = True
            if any(k in _prompt for k in ("bench_common", "统一框架", "统一测量", "共享框架", "shared framework", "统一 CSV", "统一计时")):
                _refs_shared = True
            _produces = any(
                isinstance(o, str) and _re.match(r"results_[a-z_0-9]+\.csv$", o.strip().lower())
                for o in _outs
            )
            if _produces and _kind in ("code", "coding", ""):
                _csv_producers.append(_tid)
        if _has_infra or _refs_shared:
            return None
        if len(_csv_producers) >= 3:
            return {"issue": "missing_unified_benchmark_framework", "task_ids": _csv_producers[:20]}
    except Exception:
        pass
    return None


def _algos(names, hard=False):
    out = []
    for a in names:
        out.append({"task_id": a, "kind": "code", "mode": "easy", "prompt": "x",
                    "expected_outputs": [f"{a}.c", f"results_{a}.csv"]})
        if hard:
            out.append({"task_id": f"{a}_hard", "kind": "code", "mode": "hard",
                        "prompt": "x", "expected_outputs": [f"results_{a}.csv"]})
    return out


def test_six_algos_no_framework_flagged():
    r = _detect_missing_unified_framework(_algos(["rbtree", "avl", "skiplist", "btree", "bplus", "hat"], hard=True))
    assert r and len(r["task_ids"]) == 6  # hard 副本不计


def test_with_infra_task_passes():
    c = _algos(["rbtree", "avl", "btree"]) + [
        {"task_id": "bench_infra", "kind": "code", "mode": "easy",
         "prompt": "建框架", "expected_outputs": ["bench_common.h"]}]
    assert _detect_missing_unified_framework(c) is None


def test_prompt_refs_shared_framework_passes():
    c = [{"task_id": a, "kind": "code", "mode": "easy", "prompt": "用统一框架 bench_common.h",
          "expected_outputs": [f"results_{a}.csv"]} for a in ["rbtree", "avl", "btree"]]
    assert _detect_missing_unified_framework(c) is None


def test_two_algos_below_threshold_passes():
    assert _detect_missing_unified_framework(_algos(["rbtree", "avl"])) is None


def test_resume_passes():
    c = [dict(x, resume=True) for x in _algos(["rbtree", "avl", "btree"])]
    assert _detect_missing_unified_framework(c) is None


def test_non_code_csv_not_counted():
    # draw/edit helper 产 csv 不算算法对比
    c = [{"task_id": a, "kind": "draw", "mode": "easy", "prompt": "x",
          "expected_outputs": [f"results_{a}.csv"]} for a in ["a", "b", "c"]]
    assert _detect_missing_unified_framework(c) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_framework_fallback: {len(fns)} passed")
