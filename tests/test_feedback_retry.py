"""
build_feedback_retry_text 特征测试(从 orchestrate() 内联块抽出的纯函数)。
测函数真实行为:触发条件、bot_log 抽取与截断、未触发返回空。可离线运行。
抽取时已用「exec 原内联块作 oracle」做过 7/7 逐字符等价校验(见 INTEGRATION_NOTES)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.orchestrator_prompts import build_feedback_retry_text


class _FakeHM:
    def __init__(self, role, content):
        self.role = role
        self.content = content


def test_triggered_with_bot_log_embeds_excerpt():
    hu = [_FakeHM("assistant", "ok<bot_log>complexity=hard</bot_log>done")]
    text, found = build_feedback_retry_text("\u4e0d\u5bf9", hu)  # "不对"
    assert found is True
    assert text                      # 非空
    assert "complexity=hard" in text  # bot_log 摘录被嵌入
    assert "bot_log" in text


def test_triggered_without_bot_log():
    text, found = build_feedback_retry_text("\u91cd\u505a", [_FakeHM("assistant", "no tag here")])  # "重做"
    assert found is False
    assert text  # 仍产出复盘提示(走"没找到 bot_log"分支)


def test_not_negative_feedback_returns_empty():
    text, found = build_feedback_retry_text("\u4f60\u597d", [_FakeHM("assistant", "<bot_log>x</bot_log>")])  # "你好"
    assert (text, found) == ("", False)


def test_empty_hot_user_returns_empty():
    assert build_feedback_retry_text("\u4e0d\u5bf9", []) == ("", False)


def test_bot_log_excerpt_capped_at_600():
    hu = [_FakeHM("assistant", "<bot_log>" + "y" * 800 + "</bot_log>")]
    text, found = build_feedback_retry_text("\u91cd\u505a", hu)
    assert found is True
    assert "y" * 600 in text and "y" * 601 not in text
