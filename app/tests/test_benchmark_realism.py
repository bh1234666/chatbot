"""验证 benchmark 真实性检测(2026-05-21)。

病因(实测 trace c6e42ed6 论文"1100 倍"假象): rbtree range_query 被实现成 O(n^2)
(随 N 平方增长), 论文拿它当对手 → 得出"HAT 快 1100 倍"的夸张失真结论。
修复: _detect_benchmark_complexity_anomaly 检测某操作随 N 的标度指数, >1.6(超线性接近平方)
即报警, 让 helper 交付时就发现实现低效, 避免汇总后失真。

复刻检测逻辑(纯 stdlib), 用合成数据验证。
"""
import csv as _csv_m
import math as _math
import os
import tempfile
from collections import defaultdict as _dd


def _detect_benchmark_complexity_anomaly(csv_abs_path):
    warnings = []
    try:
        with open(csv_abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
            rows = list(_csv_m.DictReader(f))
        if len(rows) < 6:
            return warnings
        cols = {c.lower().strip(): c for c in rows[0].keys() if c}
        n_col = next((cols[k] for k in ("n", "size", "scale") if k in cols), None)
        op_col = next((cols[k] for k in ("operation", "op") if k in cols), None)
        time_col = next((cols[k] for k in ("time_ms", "time", "ms", "latency_ms") if k in cols), None)
        dist_col = next((cols[k] for k in ("distribution", "dist") if k in cols), None)
        if not (n_col and op_col and time_col):
            return warnings
        series = _dd(list)
        for r in rows:
            try:
                n = float(r.get(n_col, 0) or 0); t = float(r.get(time_col, 0) or 0)
                op = (r.get(op_col, "") or "").strip()
                dist = (r.get(dist_col, "") or "").strip() if dist_col else ""
                if n > 0 and t > 0 and op:
                    series[(op, dist)].append((n, t))
            except (ValueError, TypeError):
                continue
        for (op, dist), pts in series.items():
            uniq_n = sorted({n for n, _ in pts})
            if len(uniq_n) < 3 or uniq_n[-1] / uniq_n[0] < 10:
                continue
            by_n = _dd(list)
            for n, t in pts:
                by_n[n].append(t)
            xs = sorted(by_n)
            t0 = sorted(by_n[xs[0]])[len(by_n[xs[0]]) // 2]
            t1 = sorted(by_n[xs[-1]])[len(by_n[xs[-1]]) // 2]
            if t0 <= 0 or t1 <= 0:
                continue
            p = _math.log(t1 / t0) / _math.log(xs[-1] / xs[0])
            if p > 1.6:
                warnings.append({"issue": "benchmark_complexity_anomaly",
                                 "operation": op, "scaling_exponent": round(p, 2)})
    except (OSError, ValueError, ImportError, ZeroDivisionError):
        pass
    return warnings


def _write_csv(rows):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("N,distribution,operation,time_ms,memory_kb\n")
        for n, op, t in rows:
            f.write(f"{n},random,{op},{t},100\n")
    return path


def test_quadratic_range_query_flagged():
    # O(n^2): N×10 → 时间×100
    rows = [(1000, "range_query", 0.01), (10000, "range_query", 1.0),
            (100000, "range_query", 100.0)]
    rows += [(n, "search", 0.001) for n in (1000, 10000, 100000)]
    p = _write_csv(rows)
    try:
        ws = _detect_benchmark_complexity_anomaly(p)
        assert any(w["operation"] == "range_query" for w in ws), ws
    finally:
        os.unlink(p)


def test_linear_ops_not_flagged():
    # O(log n + k) 近线性: N×10 → 时间×~10
    rows = [(1000, "range_query", 0.01), (10000, "range_query", 0.1),
            (100000, "range_query", 1.0)]
    p = _write_csv(rows)
    try:
        ws = _detect_benchmark_complexity_anomaly(p)
        assert not ws, ws
    finally:
        os.unlink(p)


def test_insufficient_scales_skipped():
    # 只 2 个规模 → 不够估标度, 跳过
    rows = [(1000, "range_query", 0.01), (10000, "range_query", 100.0)]
    rows += [(1000, "x", 1), (10000, "x", 1), (1000, "y", 1)]
    p = _write_csv(rows)
    try:
        ws = _detect_benchmark_complexity_anomaly(p)
        assert not any(w["operation"] == "range_query" for w in ws)
    finally:
        os.unlink(p)


def test_missing_columns_safe():
    fd, path = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(path, "w") as f:
        f.write("a,b\n1,2\n3,4\n5,6\n7,8\n9,10\n11,12\n")
    try:
        assert _detect_benchmark_complexity_anomaly(path) == []
    finally:
        os.unlink(path)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_benchmark_realism: {len(fns)} passed")
