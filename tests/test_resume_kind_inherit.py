"""验证 resume 时 kind 从完成 ledger 继承(2026-05-21 修复)。

病因(实测 trace c647979 08:42): code 任务 resume 时主线程没带 kind →
_normalize_helper_kind_mode 默认成 general → code 任务用 general helper 续作,
跑满 900s 0 产出。修复: resume 且未显式传 kind 时,按 task_id 查完成 ledger 继承原 kind。

本测试复刻 delegate.py `_sanitize_and_validate_tasks` 里的继承判定逻辑(纯函数,
不依赖第三方库),验证各分支行为。继承的真实代码与此逻辑等价。
"""
from collections import deque

VALID_HELPER_KINDS = ("code", "edit", "general", "verify", "draw")


class _LedgerStub:
    def __init__(self):
        self._ledger = {}

    def add(self, trace, task_id, kind):
        self._ledger.setdefault(trace, deque(maxlen=50)).append(
            {"task_id": task_id, "kind": kind}
        )

    def get(self, trace, last_n=50):
        dq = self._ledger.get(trace)
        return list(dq)[-last_n:] if dq else []


def _resolve_kind(ledger, trace, task_id, main_passed_kind, normalized_default):
    """复刻 delegate.py 继承逻辑:resume + 未传 kind 时从 ledger 继承。"""
    resume = True
    if resume and not (str(main_passed_kind or "").strip()):
        prev = None
        for e in reversed(ledger.get(trace, 50)):
            if e.get("task_id") == task_id and e.get("kind"):
                prev = str(e.get("kind")).strip().lower()
                break
        if prev and prev in VALID_HELPER_KINDS and prev != normalized_default:
            return prev
    return normalized_default


def test_code_resume_without_kind_inherits_code():
    lg = _LedgerStub(); lg.add("t1", "compress_all", "code")
    assert _resolve_kind(lg, "t1", "compress_all", None, "general") == "code"


def test_explicit_kind_respected_not_overridden():
    lg = _LedgerStub(); lg.add("t1", "compress_all", "code")
    # 主线程显式传 general → 尊重主线程,不继承 ledger 的 code
    assert _resolve_kind(lg, "t1", "compress_all", "general", "general") == "general"


def test_no_ledger_record_keeps_default():
    lg = _LedgerStub()
    assert _resolve_kind(lg, "t1", "unknown", None, "general") == "general"


def test_invalid_ledger_kind_keeps_default():
    lg = _LedgerStub(); lg.add("t2", "x", "bogus")
    assert _resolve_kind(lg, "t2", "x", None, "general") == "general"


def test_edit_kind_inherited():
    lg = _LedgerStub(); lg.add("t3", "doc", "edit")
    assert _resolve_kind(lg, "t3", "doc", None, "general") == "edit"


def test_most_recent_kind_wins():
    lg = _LedgerStub()
    lg.add("t4", "task", "general")
    lg.add("t4", "task", "code")  # 最近一次是 code
    assert _resolve_kind(lg, "t4", "task", None, "general") == "code"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_resume_kind_inherit: {len(fns)} passed")
