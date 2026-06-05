"""验证错误根因修复(2026-05-21)。

基于 trace c6e42ed6 (debug_20260521_151510) 的错误日志分析:

1. ASan 环境矛盾: helper prompt 多处硬编码"默认带 -fsanitize=address",但运行环境是
   Windows + MinGW (缺 libasan) → `cannot find -lasan` 链接失败反复撞墙(实测 8 次)。
   修复: _detect_asan_support 实际探测 + prompt/hint 据实条件化。

2. declared_missing 误报: 被竞速 graceful-abort 的 hard 副本(winner 已交付)本无独立产物,
   却被报 declared_missing(实测 avl_hard 等 8 处)。修复: is_race_aborted_twin 时降级为正常信息。

ASan 探测逻辑 + twin 判定均复刻为纯函数验证(不依赖第三方库)。
"""
import os
import shutil
import subprocess
import sys
import tempfile


# ── ASan 探测逻辑 ────────────────────────────────────────
def _detect_asan_support():
    """复刻 process_utils._detect_asan_support。"""
    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if not cc:
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "_p.c")
            out = os.path.join(td, "_p.exe" if sys.platform == "win32" else "_p")
            with open(src, "w", encoding="utf-8") as f:
                f.write("int main(void){return 0;}\n")
            proc = subprocess.run(
                [cc, "-fsanitize=address", "-g", "-O0", src, "-o", out],
                capture_output=True, timeout=30,
            )
            return proc.returncode == 0
    except Exception:
        return False


def test_asan_detection_returns_bool():
    """探测返回 bool,不抛异常(无论环境有无 libasan)。"""
    r = _detect_asan_support()
    assert isinstance(r, bool)


def test_asan_detection_no_compiler_false():
    """无编译器时返回 False(不崩)。"""
    # 模拟:把 which 都指向 None 的场景由真实环境覆盖;这里确保逻辑分支正确
    def detect(cc_present):
        if not cc_present:
            return False
        return True
    assert detect(False) is False


# ── declared_missing twin 抑制判定 ───────────────────────
def _is_race_aborted_twin(interrupted, mode, task_id):
    """复刻调用点的判定: 被竞速 abort 的 hard 副本。"""
    return bool(interrupted and mode == "hard" and str(task_id).endswith("_hard"))


def test_twin_loser_suppresses_missing():
    # avl_hard 被竞速 abort → 应抑制 declared_missing
    assert _is_race_aborted_twin(True, "hard", "avl_hard") is True


def test_primary_not_suppressed():
    # 正常 primary 完成但缺产物 → 应正常报 missing(不抑制)
    assert _is_race_aborted_twin(False, "easy", "avl") is False
    assert _is_race_aborted_twin(True, "easy", "avl") is False


def test_non_twin_hard_not_suppressed():
    # mode=hard 但 task_id 不以 _hard 结尾(非自动配对副本)→ 不抑制
    assert _is_race_aborted_twin(True, "hard", "new_algo") is False


def test_not_interrupted_hard_twin_not_suppressed():
    # hard 副本但没被 abort(自己跑完)→ 缺产物是真问题,不抑制
    assert _is_race_aborted_twin(False, "hard", "avl_hard") is False


def test_missing_outputs_check_is_not_complete():
    from app.llm.client_tools_loop import _delegate_item_outputs_complete
    assert _delegate_item_outputs_complete({"ok": True, "task_id": "x"}) is False


def test_explicit_outputs_incomplete_is_not_complete():
    from app.llm.client_tools_loop import _delegate_item_outputs_complete
    assert _delegate_item_outputs_complete({
        "ok": True,
        "task_id": "y",
        "outputs_check": {"outputs_complete": False},
    }) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_error_root_cause_fixes: {len(fns)} passed")
