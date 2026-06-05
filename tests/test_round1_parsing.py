import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_round1_accepts_structured_tendency_scores(monkeypatch):
    from app.core import orchestrator

    async def fake_chat_json(*args, **kwargs):
        return {
            "tendencies": {
                "任务委托": {"score": 0.8},
                "严肃询问": {"confidence": "0.4"},
                "噪声": {"unexpected": 9},
            },
            "rationale": "structured score",
            "complexity": "medium",
            "needs_tools": True,
            "needs_recall": False,
            "parallelizable": False,
            "is_coding_task": True,
            "is_document_task": False,
        }

    monkeypatch.setattr(orchestrator.llm, "chat_json", fake_chat_json)
    result = await orchestrator._round1("tester", "build it", [])

    assert result.tendency.tendencies["任务委托"] == 0.8
    assert result.tendency.tendencies["严肃询问"] == 0.4
    assert result.tendency.tendencies["噪声"] == 0.0
    assert result.needs_tools is True
    assert result.parallelizable is False
