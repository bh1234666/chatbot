import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Settings


def test_napcat_and_chatbot_urls_are_configurable(monkeypatch):
    monkeypatch.setenv("NAPCAT_URL", "http://napcat.example")
    monkeypatch.setenv("CHATBOT_URL", "http://chatbot.example")

    settings = Settings()

    assert settings.napcat_url == "http://napcat.example"
    assert settings.chatbot_url == "http://chatbot.example"


def test_service_and_debug_flags_are_configurable(monkeypatch):
    monkeypatch.setenv("STRICT_ACTIVE_ARCHIVE", "false")
    monkeypatch.setenv("DEBUG_MODE", "true")
    monkeypatch.setenv("DEBUG_VERBOSE", "true")
    monkeypatch.setenv("DEBUG_LOG_DIR", "logs/debug")
    monkeypatch.setenv("TOOL_RESULT_TIMESTAMP_MODE", "minimal")
    monkeypatch.setenv("LITE_ROUND3_FOR_EASY", "false")

    settings = Settings()

    assert settings.strict_active_archive is False
    assert settings.debug_mode is True
    assert settings.debug_verbose is True
    assert settings.debug_log_dir == "logs/debug"
    assert settings.tool_result_timestamp_mode == "minimal"
    assert settings.lite_round3_for_easy is False


def test_default_service_safety_flags():
    settings = Settings()

    assert settings.strict_active_archive is True
    assert settings.napcat_url == "http://localhost:8099"
    assert settings.chatbot_url == "http://localhost:8000"
