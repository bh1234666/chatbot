"""验证 granularity warning 去重(2026-05-21)。

问题: overly_broad_task (P18.C, medium, prompt≥2500 或 expected≥4) 与
single_helper_too_wide (P18, high, 枚举信号≥2 或 极长+≥1信号) 对同一 task 会同时触发,
产生两条语义重叠的"任务过宽该拆"建议。high 那条更具体(带并行拆分示例)且覆盖 medium 全部
语义 → 修复: single_helper_too_wide 触发时移除同 task 的 overly_broad_task。

复刻去重逻辑(纯函数),验证不误伤其他 task / 其他 issue。
"""


def _dedup_on_single_helper_too_wide(warnings, tid, high_fires):
    """复刻 delegate.py 去重:high 触发时移除同 task 的 overly_broad_task。"""
    if high_fires:
        warnings[:] = [
            w for w in warnings
            if not (w.get("task_id") == tid and w.get("issue") == "overly_broad_task")
        ]
        warnings.append({"task_id": tid, "issue": "single_helper_too_wide", "severity": "high"})
    return warnings


def test_both_fire_only_high_remains():
    w = [{"task_id": "t1", "issue": "overly_broad_task", "severity": "medium"}]
    _dedup_on_single_helper_too_wide(w, "t1", True)
    issues = [x["issue"] for x in w if x["task_id"] == "t1"]
    assert issues == ["single_helper_too_wide"]


def test_other_task_not_affected():
    w = [{"task_id": "t2", "issue": "overly_broad_task", "severity": "medium"}]
    _dedup_on_single_helper_too_wide(w, "t1", True)
    assert sum(1 for x in w if x["task_id"] == "t2" and x["issue"] == "overly_broad_task") == 1


def test_high_not_fire_keeps_medium():
    w = [{"task_id": "t1", "issue": "overly_broad_task", "severity": "medium"}]
    _dedup_on_single_helper_too_wide(w, "t1", False)
    assert any(x["issue"] == "overly_broad_task" for x in w)


def test_other_issue_preserved():
    w = [
        {"task_id": "t1", "issue": "kind_mismatch"},
        {"task_id": "t1", "issue": "overly_broad_task"},
    ]
    _dedup_on_single_helper_too_wide(w, "t1", True)
    assert any(x["issue"] == "kind_mismatch" for x in w)
    assert not any(x["issue"] == "overly_broad_task" for x in w)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_granularity_dedup: {len(fns)} passed")
