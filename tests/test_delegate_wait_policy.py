from app.llm.tools.delegate_wait import _zero_result_wait_extension_seconds


def test_zero_result_wait_extension_prefers_capability_over_fast_wakeup():
    assert _zero_result_wait_extension_seconds(30) == 180
    assert _zero_result_wait_extension_seconds(90) == 180
    assert _zero_result_wait_extension_seconds(180) == 300
    assert _zero_result_wait_extension_seconds(400) == 300
