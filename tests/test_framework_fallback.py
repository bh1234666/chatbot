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


def test_framework_block_shape_rejects_mixed_pipeline():
    from app.llm.tools.delegate import _is_true_horizontal_framework_block

    c = [
        {"task_id": "graph_impl", "kind": "code", "mode": "easy",
         "prompt": "实现图算法库，包含 Dijkstra、A*、MST 和 max-flow",
         "expected_outputs": ["graph_algorithms.py"]},
        {"task_id": "test_graph", "kind": "code", "mode": "easy",
         "prompt": "为 graph_algorithms.py 写单元测试并运行",
         "expected_outputs": ["test_graph_algorithms.py"]},
        {"task_id": "benchmark", "kind": "code", "mode": "easy",
         "prompt": "基于实现跑 benchmark，输出结果 CSV",
         "expected_outputs": ["bench_graph.py", "results_graph.csv"]},
        {"task_id": "report_md", "kind": "edit", "mode": "easy",
         "prompt": "读取 benchmark 结果并写报告",
         "expected_outputs": ["REPORT.md"]},
    ]

    assert _is_true_horizontal_framework_block(c, ["graph_impl", "test_graph", "benchmark", "report_md"]) is False


def test_framework_block_shape_accepts_peer_algorithms():
    from app.llm.tools.delegate import _is_true_horizontal_framework_block

    c = _algos(["rbtree", "avl", "skiplist", "btree"])

    assert _is_true_horizontal_framework_block(c, ["rbtree", "avl", "skiplist", "btree"]) is True


def test_embedded_peer_framework_contract_allows_self_contained_batch():
    from app.llm.tools.delegate import _has_embedded_peer_framework_contract

    c = [
        {
            "task_id": "impl_rbtree",
            "kind": "code",
            "mode": "hard",
            "prompt": (
                "写一个自包含的单文件C++程序 bench_rbtree.cpp。"
                "内嵌基准测试，输出 algorithm,operation,data_size,distribution,rep,time_ns,memory_bytes CSV。"
                "不依赖外部文件。"
            ),
            "expected_outputs": ["benchmark_rbtree.csv", "description_rbtree.txt"],
        },
        {
            "task_id": "impl_skiplist",
            "kind": "code",
            "mode": "hard",
            "prompt": (
                "写一个自包含的单文件C++程序 bench_skiplist.cpp。"
                "内嵌同一基准测试，输出 algorithm,operation,data_size,distribution,rep,time_ns,memory_bytes CSV。"
                "不依赖外部文件。"
            ),
            "expected_outputs": ["benchmark_skiplist.csv", "description_skiplist.txt"],
        },
        {
            "task_id": "impl_btree",
            "kind": "code",
            "mode": "hard",
            "prompt": (
                "写一个自包含的单文件C++程序 bench_btree.cpp。"
                "内嵌同一基准测试，输出 algorithm,operation,data_size,distribution,rep,time_ns,memory_bytes CSV。"
                "不依赖外部文件。"
            ),
            "expected_outputs": ["benchmark_btree.csv", "description_btree.txt"],
        },
    ]

    assert _has_embedded_peer_framework_contract(c, ["impl_rbtree", "impl_skiplist", "impl_btree"]) is True


def test_embedded_peer_framework_contract_rejects_missing_protocol():
    from app.llm.tools.delegate import _has_embedded_peer_framework_contract

    c = _algos(["rbtree", "avl", "skiplist"])

    assert _has_embedded_peer_framework_contract(c, ["rbtree", "avl", "skiplist"]) is False


def test_embedded_peer_framework_contract_accepts_chinese_analysis_contract():
    from app.llm.tools.delegate import _has_embedded_peer_framework_contract

    framework = (
        "## 论文框架契约\n"
        "论文比较红黑树、跳表、B树、B+树和AHIT五种数据结构算法。"
        "每份算法分析须包含: 1)数据结构精确定义(节点结构、全局属性) "
        "2)查找/插入/删除操作伪代码+复杂度推导 3)空间复杂度分析 "
        "4)适用场景(至少3个具体例子) 5)优缺点总结。"
        "输出为文本证据文件，中文撰写。比较表格schema: "
        "算法|查找(平均/最坏)|插入(平均/最坏)|删除(平均/最坏)|空间|平衡性|范围查询|适用场景。"
    )
    tasks = [
        {"task_id": task_id, "kind": "read", "framework": framework, "prompt": "按框架分析。", "expected_outputs": [f"{task_id}.txt"]}
        for task_id in ["rb_tree_analysis", "skip_list_analysis", "b_tree_analysis", "bplus_tree_analysis", "ahit_analysis"]
    ]

    assert _has_embedded_peer_framework_contract(tasks, [t["task_id"] for t in tasks]) is True


def test_embedded_peer_framework_contract_accepts_structured_template_contract():
    from app.llm.tools.delegate import _has_embedded_peer_framework_contract

    framework = {
        "from_file": "paper_framework_spec.json",
        "role": "红黑树分析",
        "output": "rb_tree_analysis.txt",
        "template": {
            "required_subsections": ["数据结构定义", "查找操作", "插入操作", "删除操作", "空间复杂度", "适用场景", "优缺点"],
            "pseudocode_style": "标准伪代码",
            "complexity": "大O",
        },
        "acceptance": ["维度完整", "术语一致", "可合并"],
    }
    tasks = [
        {"task_id": task_id, "kind": "read", "framework": framework, "prompt": "按框架分析。", "expected_outputs": [f"{task_id}.txt"]}
        for task_id in ["rb_tree_analysis", "skip_list_analysis", "b_tree_analysis", "bplus_tree_analysis", "ahit_analysis"]
    ]

    assert _has_embedded_peer_framework_contract(tasks, [t["task_id"] for t in tasks]) is True


def test_broad_framework_guard_blocks_overconcentrated_task_even_with_framework():
    from app.llm.tools.delegate_framework import broad_framework_guard_warnings

    framework = {
        "goal": "Write a database-index paper with shared comparison dimensions.",
        "schema": "algorithm | lookup | insert | delete | space | range query | scenario",
        "output": "chapter markdown files and final docx",
        "acceptance": ["consistent dimensions", "mergeable sections"],
    }
    tasks = [{
        "task_id": "algo_comparison",
        "kind": "code",
        "mode": "hard",
        "framework": framework,
        "prompt": (
            "Write a deep comparison chapter for Red-Black Tree, Skip List, B-Tree, and B+ Tree. "
            "For every algorithm include definition, node structure, lookup/insert/delete pseudocode, "
            "average and worst-case complexity, space cost, cache locality, concurrency, range query support, "
            "database scenarios, advantages, disadvantages, and a unified markdown comparison table. "
            "Each algorithm section should be 500-800 Chinese characters and include reasoning for every dimension. "
            "Also prepare merge notes for the final rigorous academic paper."
        ),
        "expected_outputs": ["classic_algorithm_comparison.md"],
    }]

    warnings = broad_framework_guard_warnings(tasks)

    assert any(
        w.get("issue") == "overconcentrated_helper_task"
        and w.get("observed_framework_state") == "has_framework"
        and w.get("observed_framework_boundary_fact")
        for w in warnings
    )


def test_broad_framework_guard_counts_chinese_algorithm_units():
    from app.llm.tools.delegate_framework import broad_framework_guard_warnings

    framework = {
        "goal": "完成数据库索引论文分章素材。",
        "schema": "算法|结构|查找|插入|删除|空间|范围查询|场景",
        "output": "章节 markdown",
        "acceptance": ["维度完整", "可合并"],
    }
    tasks = [{
        "task_id": "known_algo_analysis",
        "kind": "code",
        "mode": "hard",
        "framework": framework,
        "prompt": "\u5206\u6790\u56db\u79cd\u5df2\u77e5\u6570\u636e\u7ed3\u6784\u7b97\u6cd5"
        "\uff08\u7ea2\u9ed1\u6811\u3001\u8df3\u8868\u3001B\u6811\u3001B+\u6811\uff09"
        "\uff0c\u751f\u6210\u8be6\u7ec6\u7684\u7b97\u6cd5\u5206\u6790\u6587\u6863\u548c"
        "\u6bd4\u8f83\u8868\u683c\u3002\u6bcf\u79cd\u7b97\u6cd5\u90fd\u8981\u5305\u542b"
        "\u7ed3\u6784\u5b9a\u4e49\u3001\u67e5\u627e/\u63d2\u5165/\u5220\u9664"
        "\u4f2a\u4ee3\u7801\u3001\u590d\u6742\u5ea6\uff0c\u5e76\u4e3a\u6700\u7ec8"
        "\u8bba\u6587\u51c6\u5907\u53ef\u5408\u5e76\u7d20\u6750\u3002",
        "expected_outputs": ["known_algorithm_analysis.md"],
    }]

    warnings = broad_framework_guard_warnings(tasks)

    assert any(
        w.get("issue") == "overconcentrated_helper_task"
        and w.get("observed_framework_state") == "has_framework"
        and w.get("observed_framework_boundary_fact")
        for w in warnings
    )


def test_framework_block_shape_rejects_chart_consumer_pair():
    from app.llm.tools.delegate import _is_true_horizontal_framework_block

    c = [
        {"task_id": "raft_bench_impl", "kind": "code", "mode": "easy",
         "prompt": "实现 Raft benchmark 并输出 CSV",
         "expected_outputs": ["raft_bench.py", "results_raft.csv"]},
        {"task_id": "raft_bench_chart", "kind": "draw", "mode": "easy",
         "prompt": "读取 results_raft.csv 画性能图",
         "expected_outputs": ["raft_chart.png"]},
    ]

    assert _is_true_horizontal_framework_block(c, ["raft_bench_impl", "raft_bench_chart"]) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_framework_fallback: {len(fns)} passed")
