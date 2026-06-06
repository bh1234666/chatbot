"""验证 dream 后台任务优化(2026-05-21)。

基于 trace c647979 日志分析发现的两个问题:
  1. d3_kb_placeholder.info_fn 在高频 supervisor wake 下被调 243 次,每次做 500 行全表
     扫描,结论几乎全是 orphan=0("不用跑")→ 加 TTL 缓存把重复全扫降到每 ~45s 一次。
  2. d24_refine 连续 timeout early_abort 后 break 正常返回,框架当成功处理(重置失败计数+
     更新 watermark),下次 wake 立刻重跑再撞 3×30s 墙 → 加 early_abort 退避(suspended_until)。

两段逻辑均复刻为纯函数验证(不依赖 DB/LLM)。真实代码与此逻辑等价。
"""
import asyncio
import time
from types import SimpleNamespace

from app.core.dream.supervisor import _limit_ready_tasks_for_cycle
from app.core.dream import supervisor as dream_supervisor_mod
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.kb_dag.d24_refine import (
    D24_NODE_BACKOFF_BASE_SEC,
    D24_NODE_BACKOFF_MAX_SEC,
    D24_MAX_PER_RUN,
    D24_SOURCE_MSG_CHARS,
    D24_SOURCE_MSG_LIMIT,
    _d24_node_backoff_seconds,
    _d24_node_failure_count,
    _d24_node_is_backed_off,
    _load_node_metadata,
)
from app.core.dream.tasks.file_searchability.file_meta import is_active_file_metadata
from app.core.dream.tasks.kb_dag import d21_node_merge, d22_node_split, d23_high_level_abstract, d25_edges
from app.core.dream.tasks.file_searchability import d15_image_index, d16_pdf_deep, d17_office_index
from app.core.dream.tasks.memory_maintenance import d4_workspace_cleanup
from app.memory import kb as kb_memory


# ── d3 info_fn TTL 缓存 ──────────────────────────────────
class _D3CacheStub:
    _INFO_CACHE_TTL_SEC = 45.0

    def __init__(self):
        self.scan_count = 0

    async def _info_fn_uncached(self):
        self.scan_count += 1  # 模拟 500 行全表扫描
        return 0.0

    async def info_fn(self):
        _now = time.time()
        _cached_at = getattr(self, "_info_cache_at", 0.0)
        if _now - _cached_at < self._INFO_CACHE_TTL_SEC:
            return getattr(self, "_info_cache_val", 0.0)
        _val = await self._info_fn_uncached()
        self._info_cache_at = _now
        self._info_cache_val = _val
        return _val


def test_d3_cache_collapses_repeated_scans():
    async def run():
        d = _D3CacheStub()
        for _ in range(243):  # 模拟 243 次 supervisor wake
            await d.info_fn()
        return d.scan_count
    assert asyncio.run(run()) == 1  # 243 次调用只触发 1 次全扫


def test_d3_cache_invalidation_rescan():
    async def run():
        d = _D3CacheStub()
        await d.info_fn()
        d._info_cache_at = 0.0  # _do_work 后失效
        await d.info_fn()
        return d.scan_count
    assert asyncio.run(run()) == 2


def test_d3_cache_ttl_expiry_rescan():
    async def run():
        d = _D3CacheStub()
        await d.info_fn()
        d._info_cache_at = time.time() - 46  # TTL 过期
        await d.info_fn()
        return d.scan_count
    assert asyncio.run(run()) == 2


# ── d24 early_abort 退避 ─────────────────────────────────
def _d24_should_backoff(early_aborted: bool, success: int) -> bool:
    """复刻 d24_refine 退避判定:连续失败 abort 且零成功 → 退避。"""
    return early_aborted and success == 0


def test_d24_backoff_on_abort_zero_success():
    assert _d24_should_backoff(True, 0) is True


def test_d24_no_backoff_when_partial_success():
    assert _d24_should_backoff(True, 2) is False


def test_d24_no_backoff_when_not_aborted():
    assert _d24_should_backoff(False, 0) is False
    assert _d24_should_backoff(False, 5) is False


def test_d24_load_node_metadata_tolerates_bad_values():
    assert _load_node_metadata('{"refined": true}') == {"refined": True}
    assert _load_node_metadata("{bad json") == {}
    assert _load_node_metadata(["not", "a", "dict"]) == {}


def test_d24_node_backoff_is_exponential_and_capped():
    assert _d24_node_backoff_seconds(1) == D24_NODE_BACKOFF_BASE_SEC
    assert _d24_node_backoff_seconds(2) == D24_NODE_BACKOFF_BASE_SEC * 6
    assert _d24_node_backoff_seconds(3) == D24_NODE_BACKOFF_MAX_SEC
    assert _d24_node_backoff_seconds(99) == D24_NODE_BACKOFF_MAX_SEC


def test_d24_node_backoff_predicate_and_failure_count_are_robust():
    now = time.time()
    assert _d24_node_is_backed_off({"d24_refine_backoff_until": now + 1}, now) is True
    assert _d24_node_is_backed_off({"d24_refine_backoff_until": now - 1}, now) is False
    assert _d24_node_is_backed_off({"d24_refine_backoff_until": "bad"}, now) is False
    assert _d24_node_failure_count({"d24_refine_failures": "3"}) == 3
    assert _d24_node_failure_count({"d24_refine_failures": "bad"}) == 0


def test_d24_refine_uses_small_batches_and_small_source_context():
    assert D24_MAX_PER_RUN <= 4
    assert D24_SOURCE_MSG_LIMIT <= 3
    assert D24_SOURCE_MSG_CHARS <= 120


class _StartupSweepTask(InfoDrivenTask):
    name = "startup_sweep_test"
    threshold = 10
    startup_sweep = False

    async def info_fn(self):
        return 0

    async def _do_work(self):
        pass


class _StartupSweepEnabledTask(_StartupSweepTask):
    name = "startup_sweep_enabled_test"
    startup_sweep = True


def test_startup_sweep_is_opt_in():
    async def run():
        default = _StartupSweepTask()
        enabled = _StartupSweepEnabledTask()
        return await default.should_run(), await enabled.should_run()
    assert asyncio.run(run()) == (False, True)


def test_dream_cycle_selects_all_non_conflicting_tasks_without_llm_limit(monkeypatch):
    tasks = [
        SimpleNamespace(name="d15_image_index", uses_llm=True),
        SimpleNamespace(name="d16_pdf_deep", uses_llm=True),
        SimpleNamespace(name="d21_merge", uses_llm=True),
        SimpleNamespace(name="d1_hot_to_warm", uses_llm=True),
        SimpleNamespace(name="d5_artifact_cleanup", uses_llm=False),
        SimpleNamespace(name="d24_refine", uses_llm=True),
    ]
    selected, deferred = _limit_ready_tasks_for_cycle(tasks)
    assert [t.name for t in selected] == [
        "d15_image_index",
        "d16_pdf_deep",
        "d21_merge",
        "d1_hot_to_warm",
        "d5_artifact_cleanup",
    ]
    assert [t.name for t in deferred] == ["d24_refine"]
    assert sum(1 for t in selected if t.uses_llm) == 4


def test_dream_cycle_file_first_and_single_kb_maintenance(monkeypatch):
    tasks = [
        SimpleNamespace(name="d15_image_index", uses_llm=True),
        SimpleNamespace(name="d16_pdf_deep", uses_llm=True),
        SimpleNamespace(name="d21_merge", uses_llm=True),
        SimpleNamespace(name="d22_split", uses_llm=True),
        SimpleNamespace(name="d1_hot_to_warm", uses_llm=True),
        SimpleNamespace(name="d6_sqlite_wal", uses_llm=False),
    ]
    selected, deferred = _limit_ready_tasks_for_cycle(tasks)
    assert [t.name for t in selected] == [
        "d15_image_index",
        "d16_pdf_deep",
        "d21_merge",
        "d1_hot_to_warm",
        "d6_sqlite_wal",
    ]
    assert [t.name for t in deferred] == ["d22_split"]


def test_dream_d23_accepts_supported_subset_from_noisy_cluster():
    input_ids = {f"c_{i}" for i in range(8)}
    raw = {
        "skip": False,
        "headline": "Redlock lease failure pattern",
        "content": "Multiple nodes describe lock lease failures around GC pause and timeout boundaries, making this a useful retrieval topic for later distributed lock debugging.",
        "topic_type": "pattern",
        "subset_node_ids": ["c_0", "c_1", "c_2", "c_3", "c_4"],
        "edge_weights": {"c_0": 0.8, "c_1": "0.6"},
    }
    normalized = d23_high_level_abstract._normalize_d23_output(raw, input_ids)

    assert normalized is not None
    assert set(normalized["subset_node_ids"]) == {"c_0", "c_1", "c_2", "c_3", "c_4"}
    assert set(normalized["edge_weights"]) == set(normalized["subset_node_ids"])
    assert normalized["edge_weights"]["c_2"] == 0.5


def test_dream_d25_filters_bad_edges_instead_of_rejecting_batch():
    raw = {
        "new_edges": [
            {"src_id": "c_a", "dst_id": "c_b", "weight": 0.7, "reason": "related"},
            {"src_id": "c_a", "dst_id": "outside", "weight": 0.7},
            {"src_id": "c_a", "dst_id": "c_a", "weight": 0.7},
        ],
        "boost_edges": [
            {"src_id": "c_b", "dst_id": "c_c", "new_weight": "0.8"},
            {"src_id": "c_c", "dst_id": "c_b", "new_weight": 2.0},
        ],
    }
    normalized = d25_edges._normalize_d25_output(raw, {"c_a", "c_b", "c_c"})

    assert normalized == {
        "new_edges": [{"src_id": "c_a", "dst_id": "c_b", "reason": "related", "weight": 0.7}],
        "boost_edges": [{"src_id": "c_b", "dst_id": "c_c", "reason": "", "new_weight": 0.8}],
    }


def test_dream_does_not_start_while_main_request_active(monkeypatch):
    monkeypatch.setattr(dream_supervisor_mod.settings, "dream_enabled", True)
    monkeypatch.setattr(dream_supervisor_mod, "_shutdown_event", SimpleNamespace(is_set=lambda: False))
    monkeypatch.setattr(dream_supervisor_mod, "_last_main_activity", time.time() - 999)
    monkeypatch.setattr(dream_supervisor_mod, "_active_main_requests", 1)

    assert dream_supervisor_mod._can_dream() is False


def test_kb_compress_context_soft_limit_is_below_large_context():
    assert kb_memory._KB_COMPRESS_CONTEXT_SOFT_CHARS == 200_000
    assert 1 <= kb_memory._KB_COMPRESS_MIN_BATCH <= 10
    assert kb_memory._KB_COMPRESS_SMALL_BATCH_TTL_SEC >= 300


def test_file_searchability_filters_inactive_file_metadata():
    assert is_active_file_metadata({"download_status": "done"}) is True
    assert is_active_file_metadata({"deleted": True, "download_status": "done"}) is False
    assert is_active_file_metadata({"deleted": "true", "download_status": "done"}) is False
    assert is_active_file_metadata({"download_status": "failed"}) is False


def test_file_indexed_contributes_to_kb_maintenance_signal(monkeypatch):
    from app.core.dream.event_bus import event_bus
    from app.core.dream.tasks.kb_dag.signals import kb_maintenance_signal_count

    monkeypatch.setattr(event_bus, "_total_count", {"kb_nodes_added": 2, "file_indexed": 3})
    assert kb_maintenance_signal_count() == 5.0
    from app.core.dream.tasks.kb_dag.signals import file_indexed_count
    assert file_indexed_count() == 3.0


def test_dream_llm_prompts_are_compact_and_not_mojibake():
    prompts = [
        d15_image_index._LLM_PROMPT_SYSTEM,
        d16_pdf_deep._LLM_PROMPT_SYSTEM,
        d17_office_index._LLM_PROMPT,
        d21_node_merge._LLM_PROMPT_SYSTEM,
        d22_node_split._LLM_PROMPT,
        d23_high_level_abstract._LLM_PROMPT_SYSTEM,
        d25_edges._LLM_PROMPT,
        d4_workspace_cleanup._LLM_PROMPT,
    ]
    for prompt in prompts:
        assert len(prompt) < 1800
        assert "strict JSON" in prompt or "Return strict JSON" in prompt
        assert "鈫" not in prompt
        assert "鍥" not in prompt


def test_d4_workspace_cleanup_prompt_formats_json_example():
    prompt = d4_workspace_cleanup._LLM_PROMPT.format(
        archive="archive",
        group="group",
        agent_mb=123,
        aggressiveness="low",
        n=1,
        candidates="- task_id=orphan:old.txt, kind=orphan_file, age=10h, size=1MB",
    )

    assert '"decisions"' in prompt
    assert "{agent_mb}" not in prompt
    assert "orphan:old.txt" in prompt


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_dream_task_optimizations: {len(fns)} passed")
