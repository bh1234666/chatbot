import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_round2_stage_table_uses_model_pool_tasks():
    from app.core.round2_stage import R2_STAGE_TABLE
    from app.llm.model_pool import TASK_TIER

    expected = {
        "medium": "round2_medium",
        "medium_coding": "round2_medium_coding",
        "hard": "round2_hard",
        "veryhard": "round2_veryhard",
    }
    assert set(R2_STAGE_TABLE) == set(expected)
    for stage, task_name in expected.items():
        cfg = R2_STAGE_TABLE[stage]
        assert cfg["task_name"] == task_name
        assert task_name in TASK_TIER
        assert "think" not in cfg
        assert "tier" not in cfg
