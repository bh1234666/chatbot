import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_botctl_api_uses_chatbot_url_env(monkeypatch):
    monkeypatch.setenv("CHATBOT_URL", "http://botctl.example/root")
    sys.modules.pop("botctl_helper", None)

    botctl_helper = importlib.import_module("botctl_helper")

    assert botctl_helper.API == "http://botctl.example/root/v1"


def test_language_detection_and_directive():
    from app.core.language import detect_user_language, language_directive

    assert detect_user_language("请帮我整理这份中文报告") == "zh"
    assert detect_user_language("hello world this is english") == "en"
    assert detect_user_language("please 用中文 summarize this") == "mixed"
    zh_directive = language_directive("zh")
    mixed_directive = language_directive("mixed")

    assert "必须中文" in zh_directive or "should be in Chinese" in zh_directive
    assert "matplotlib 中文绘图约束" in zh_directive or "Matplotlib Chinese" in zh_directive
    assert "⁰" not in zh_directive
    assert "中英混合" in mixed_directive
    assert language_directive("en") == ""


def test_precheck_script_exists_with_expected_scope():
    script = Path(__file__).parent.parent / "scripts" / "precheck.ps1"
    text = script.read_text(encoding="utf-8")

    assert "tests/test_smoke.py" in text
    assert "tests/test_model_pool.py" in text
    assert "tests/test_structure.py" in text
    assert "claude-code-main" not in text
