# -*- coding: utf-8 -*-
"""Shared GPU resource gates for heavyweight local tools."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Callable

from app.config import settings


_HELD_COUNTS: ContextVar[dict[str, int]] = ContextVar("gpu_resource_held_counts", default={})


def _positive(value: int | None, default: int = 1) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(1, n)


def _nonnegative(value: int | None, default: int = 0) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(0, n)


def _count(name: str) -> int:
    return int(_HELD_COUNTS.get().get(name, 0) or 0)


def _set_count(name: str, count: int) -> None:
    counts = dict(_HELD_COUNTS.get())
    if count > 0:
        counts[name] = count
    else:
        counts.pop(name, None)
    _HELD_COUNTS.set(counts)


class ReentrantResourceSemaphore:
    """A context-aware semaphore shared by sync and async tool paths."""

    def __init__(self, name: str, capacity: int):
        self.name = name
        self.capacity = _positive(capacity)
        self._sem = threading.BoundedSemaphore(self.capacity)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        held = _count(self.name)
        if held > 0:
            _set_count(self.name, held + 1)
            return True
        if timeout is None:
            ok = self._sem.acquire(blocking)
        else:
            ok = self._sem.acquire(blocking, timeout)
        if ok:
            _set_count(self.name, 1)
        return bool(ok)

    async def acquire_async(self) -> bool:
        held = _count(self.name)
        if held > 0:
            _set_count(self.name, held + 1)
            return True
        ok = await asyncio.to_thread(self._sem.acquire)
        if ok:
            _set_count(self.name, 1)
        return bool(ok)

    def release(self) -> None:
        held = _count(self.name)
        if held > 1:
            _set_count(self.name, held - 1)
            return
        if held == 1:
            _set_count(self.name, 0)
        self._sem.release()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"timed out waiting for GPU resource semaphore: {self.name}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class ReentrantGpuBudget:
    """Weighted in-process VRAM budget gate.

    The weights are estimates, not live NVML reservations.  They keep normal
    helper scheduling below the configured project budget while still letting
    operators tune per-tool costs after empirical measurements.
    """

    def __init__(self, capacity_mb: int, costs_mb: dict[str, int]):
        self.name = "gpu.budget"
        self.capacity_mb = _positive(capacity_mb, 8000)
        self.costs_mb = {k: _nonnegative(v) for k, v in costs_mb.items()}
        self._used_mb = 0
        self._cond = threading.Condition()

    def cost(self, kind: str) -> int:
        normalized = (kind or "").strip().lower()
        return self.costs_mb.get(normalized, 0)

    def acquire(self, kind: str) -> bool:
        cost = self.cost(kind)
        if cost <= 0:
            return True
        held_name = f"{self.name}.{kind}"
        held = _count(held_name)
        if held > 0:
            _set_count(held_name, held + 1)
            return True
        with self._cond:
            while self._used_mb + cost > self.capacity_mb:
                self._cond.wait()
            self._used_mb += cost
        _set_count(held_name, 1)
        return True

    async def acquire_async(self, kind: str) -> bool:
        cost = self.cost(kind)
        if cost <= 0:
            return True
        held_name = f"{self.name}.{kind}"
        held = _count(held_name)
        if held > 0:
            _set_count(held_name, held + 1)
            return True

        def _wait_for_budget() -> None:
            with self._cond:
                while self._used_mb + cost > self.capacity_mb:
                    self._cond.wait()
                self._used_mb += cost

        await asyncio.to_thread(_wait_for_budget)
        _set_count(held_name, 1)
        return True

    def release(self, kind: str) -> None:
        cost = self.cost(kind)
        if cost <= 0:
            return
        held_name = f"{self.name}.{kind}"
        held = _count(held_name)
        if held > 1:
            _set_count(held_name, held - 1)
            return
        if held == 1:
            _set_count(held_name, 0)
        with self._cond:
            self._used_mb = max(0, self._used_mb - cost)
            self._cond.notify_all()

    @contextmanager
    def scope(self, kind: str):
        self.acquire(kind)
        try:
            yield
        finally:
            self.release(kind)

    @asynccontextmanager
    async def async_scope(self, kind: str):
        await self.acquire_async(kind)
        try:
            yield
        finally:
            self.release(kind)


class GpuResourceLimiter:
    def __init__(
        self,
        *,
        total: int,
        ocr: int,
        tts: int,
        mineru: int,
        umiocr: int,
        budget_mb: int = 8000,
        costs_mb: dict[str, int] | None = None,
    ):
        self.total = ReentrantResourceSemaphore("gpu.total", total)
        self.ocr = ReentrantResourceSemaphore("gpu.ocr", ocr)
        self.tts = ReentrantResourceSemaphore("gpu.tts", tts)
        self.mineru = ReentrantResourceSemaphore("gpu.mineru", mineru)
        self.umiocr = ReentrantResourceSemaphore("gpu.umiocr", umiocr)
        self.budget = ReentrantGpuBudget(
            budget_mb,
            costs_mb or {"ocr": 0, "tts": 2500, "mineru": 1500, "umiocr": 500},
        )

    @classmethod
    def from_settings(cls) -> "GpuResourceLimiter":
        return cls(
            total=_positive(settings.gpu_concurrency),
            ocr=_positive(settings.ocr_concurrency),
            tts=_positive(settings.tts_concurrency),
            mineru=_positive(getattr(settings, "mineru_concurrency", 1)),
            umiocr=_positive(getattr(settings, "umiocr_concurrency", 1)),
            budget_mb=_positive(getattr(settings, "gpu_memory_budget_mb", 8000), 8000),
            costs_mb={
                "ocr": _nonnegative(getattr(settings, "gpu_ocr_memory_mb", 0)),
                "tts": _nonnegative(getattr(settings, "gpu_tts_memory_mb", 2500)),
                "mineru": _nonnegative(getattr(settings, "gpu_mineru_memory_mb", 1500)),
                "umiocr": _nonnegative(getattr(settings, "gpu_umiocr_memory_mb", 500)),
            },
        )

    def _specific(self, kind: str) -> ReentrantResourceSemaphore | None:
        normalized = (kind or "").strip().lower()
        if normalized == "ocr":
            return self.ocr
        if normalized == "tts":
            return self.tts
        if normalized == "mineru":
            return self.mineru
        if normalized in {"umi", "umiocr", "legacy_ocr"}:
            return self.umiocr
        return None

    @contextmanager
    def scope(self, kind: str):
        specific = self._specific(kind)
        with self.budget.scope(kind):
            with self.total:
                if specific is None:
                    yield
                else:
                    with specific:
                        yield

    @asynccontextmanager
    async def async_scope(self, kind: str):
        specific = self._specific(kind)
        async with self.budget.async_scope(kind):
            async with async_semaphore(self.total):
                if specific is None:
                    yield
                else:
                    async with async_semaphore(specific):
                        yield

    def limits(self) -> dict[str, int]:
        return {
            "total": self.total.capacity,
            "ocr": self.ocr.capacity,
            "tts": self.tts.capacity,
            "mineru": self.mineru.capacity,
            "umiocr": self.umiocr.capacity,
            "budget_mb": self.budget.capacity_mb,
            "costs_mb": dict(self.budget.costs_mb),
        }


@asynccontextmanager
async def async_semaphore(sem):
    if hasattr(sem, "acquire_async"):
        ok = await sem.acquire_async()
    else:
        ok = await asyncio.to_thread(sem.acquire)
    if not ok:
        raise TimeoutError("timed out waiting for semaphore")
    try:
        yield
    finally:
        sem.release()


_LIMITER = GpuResourceLimiter.from_settings()

_GPU_SEMAPHORE = _LIMITER.total
_OCR_SEMAPHORE = _LIMITER.ocr
_TTS_SEMAPHORE = _LIMITER.tts
_MINERU_SEMAPHORE = _LIMITER.mineru
_UMIOCR_SEMAPHORE = _LIMITER.umiocr


def configured_gpu_limits() -> dict[str, int]:
    return _LIMITER.limits()


def gpu_resource_scope(kind: str):
    return _LIMITER.scope(kind)


async def run_gpu_task(kind: str, func: Callable, /, *args, **kwargs):
    async with _LIMITER.async_scope(kind):
        return await asyncio.to_thread(func, *args, **kwargs)


async def run_gpu_ocr(func: Callable, /, *args, **kwargs):
    return await run_gpu_task("ocr", func, *args, **kwargs)


async def run_gpu_tts(func: Callable, /, *args, **kwargs):
    return await run_gpu_task("tts", func, *args, **kwargs)


_async_semaphore = async_semaphore
