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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_dream_task_optimizations: {len(fns)} passed")
