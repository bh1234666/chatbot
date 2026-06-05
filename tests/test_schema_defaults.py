import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.api import (
    BotGroupResponse,
    GroupFileItem,
    GroupFilesSyncRequest,
    ResponsePlan,
    TendencyAnalysis,
)


def test_tendency_analysis_list_defaults_are_independent():
    first = TendencyAnalysis(tendencies={}, rationale="x")
    second = TendencyAnalysis(tendencies={}, rationale="y")

    first.recall_topics.append("topic")
    first.recall_layers.append("warm")

    assert second.recall_topics == []
    assert second.recall_layers == []


def test_response_plan_list_defaults_are_independent():
    first = ResponsePlan(intent="x", key_points=[], tone="normal", length_hint="short")
    second = ResponsePlan(intent="y", key_points=[], tone="normal", length_hint="short")

    first.avoid.append("avoid")
    first.callbacks.append("callback")
    first.deliverables.append("a.txt")
    first.delivery_partial.append("b.txt")

    assert second.avoid == []
    assert second.callbacks == []
    assert second.deliverables == []
    assert second.delivery_partial == []


def test_response_list_defaults_are_independent():
    first_group = BotGroupResponse(group_id="g1")
    second_group = BotGroupResponse(group_id="g2")
    first_group.personas.append({"archive_id": "a1"})
    assert second_group.personas == []

    item = GroupFileItem(file_id="f1", file_name="a.txt", file_size=1, upload_time=1)
    first_sync = GroupFilesSyncRequest()
    second_sync = GroupFilesSyncRequest()
    first_sync.files.append(item)
    assert second_sync.files == []


def test_model_field_defaults_use_factories_for_lists():
    for model, names in [
        (TendencyAnalysis, ["recall_topics", "recall_layers"]),
        (ResponsePlan, ["avoid", "callbacks", "deliverables", "delivery_partial"]),
        (BotGroupResponse, ["personas"]),
        (GroupFilesSyncRequest, ["files"]),
    ]:
        for name in names:
            field = model.model_fields[name]
            assert field.default_factory is not None, f"{model.__name__}.{name} should use default_factory"
