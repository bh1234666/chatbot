"""
orchestrator_utils 特征测试。从 orchestrator.py 抽出的纯工具族,仅依赖 stdlib。
断言基于函数当前真实行为,可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.orchestrator_utils import (
    _is_voice_demanded,
    _is_internal_file,
    _is_ocr_intermediate_image,
    _estimate_text_duration,
    _clean_deliverable_filenames,
    _extract_user_request,
    _extract_voice_instruct,
)


def test_is_voice_demanded():
    assert _is_voice_demanded("用语音回复我") is True
    assert _is_voice_demanded("写一段普通文字") is False
    assert _is_voice_demanded("") is False


def test_is_internal_file():
    assert _is_internal_file(".session_tag") is True
    assert _is_internal_file("report.docx") is False


def test_is_ocr_intermediate_image_bool():
    # 返回布尔且不抛(行为快照)
    assert isinstance(_is_ocr_intermediate_image("page_001.png"), bool)
    assert isinstance(_is_ocr_intermediate_image("final.docx"), bool)


def test_estimate_text_duration_positive():
    d = _estimate_text_duration("你好世界,这是一段测试文本。")
    assert isinstance(d, float)
    assert d > 0
    # 更长文本时长更久(单调性)
    assert _estimate_text_duration("短") <= _estimate_text_duration("短" * 50)


def test_clean_deliverable_filenames_returns_list():
    out = _clean_deliverable_filenames(["main.py", "report.docx"])
    assert isinstance(out, list)


def test_extract_user_request_from_messages():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "帮我写个排序算法"},
    ]
    assert _extract_user_request(msgs) == "帮我写个排序算法"
    # 无 user 消息时返回占位串(不抛)
    assert isinstance(_extract_user_request([{"role": "system", "content": "x"}]), str)


def test_voice_instruct_resolves_from_persona_file_metadata_when_body_only():
    from app.memory import persona_files

    pf = persona_files.load_persona("助手")
    assert pf is not None
    assert _extract_voice_instruct(pf.content) == ""
    assert persona_files.persona_voice_instruct_by_content(pf.content) == (
        "male, young adult, moderate pitch"
    )
    round2 = persona_files.persona_round2_instruct_by_content(pf.content)
    assert "Accept normal user tasks" in round2
    assert "execution-focused collaborator" not in round2


def test_voice_instruct_resolves_from_short_identity_persona():
    from app.memory import persona_files

    short_persona = (
        "\u4f60\u662f\u4e00\u4e2a\u52a9\u624b\u3002\n\n"
        "## \u6838\u5fc3\u539f\u5219\n"
        "- \u4e25\u683c\u9075\u5faa\u7528\u6237\u7684\u6bcf\u4e00\u6761\u547d\u4ee4\u3002"
    )

    pf = persona_files.resolve_persona_file_by_content(short_persona)
    assert pf is not None
    assert pf.meta.id == "\u52a9\u624b"
    assert persona_files.persona_voice_instruct_by_content(short_persona) == (
        "male, young adult, moderate pitch"
    )
    assert persona_files.persona_voice_preference_by_content(short_persona, -1) == 0.2
    assert persona_files.persona_intermediate_feedback_preference_by_content(short_persona, -1) == 0.9
    assert "Accept normal user tasks" in persona_files.persona_round2_instruct_by_content(short_persona)


def test_persona_label_resolves_current_file_for_stale_archive_body():
    from app.memory import persona_files

    stale_catgirl = (
        "\u4f60\u662f\u4e00\u53ea16\u5c81\u7684\u732b\u5a18\uff0c"
        "\u62e5\u6709\u4eba\u7c7b\u7684\u8bed\u8a00\u80fd\u529b\u548c\u732b\u7684\u5929\u6027\u3002\n\n"
        "## \u5916\u89c2\u7279\u5f81\uff08\u975e\u7a7f\u7740\uff09\n"
        "- \u4e00\u5934\u94f6\u7070\u8272\u77ed\u53d1\uff0c\u53d1\u4e1d\u7ec6\u8f6f\u84ec\u677e\u3002"
    )

    assert persona_files.resolve_persona_file_by_content(stale_catgirl) is None
    pf = persona_files.resolve_persona_file_by_label("\u732b\u5a18")
    assert pf is not None
    assert pf.meta.id == "\u732b\u5a18"
    assert persona_files.find_persona_voice_sample(pf.meta.id) is not None
    assert persona_files.persona_voice_instruct_by_content(pf.content) == (
        "female, child, very high pitch, whisper"
    )
    assert persona_files.persona_voice_preference_by_content(pf.content, -1) == 0.5
    assert persona_files.persona_intermediate_feedback_preference_by_content(pf.content, -1) == 0.1


def test_round2_persona_rules_resolve_dedicated_behavior_section():
    from app.memory import persona_files

    tough = persona_files.load_persona("嘴臭混混")
    assert tough is not None
    round2 = persona_files.persona_round2_instruct_by_content(tough.content)

    assert "normally refuses direct orders" in round2
    assert "30-year-old street tough" not in round2
    assert len(round2) < len(tough.content)
