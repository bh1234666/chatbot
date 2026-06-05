from stress_tools.response_quality import ineffective_reply_reasons


def test_ineffective_detection_allows_negative_factual_statements():
    text = "群文件里没有实际执行过任何脚本，也没有新增可执行文件。"

    assert ineffective_reply_reasons(text) == []


def test_ineffective_detection_allows_risk_statements():
    text = "tests/ 下的测试还是占位符，没有真正测到业务逻辑。"

    assert ineffective_reply_reasons(text) == []


def test_ineffective_detection_still_flags_self_admitted_no_work():
    text = "我没有实际处理这个文件，只是口头说了一下。"

    assert ineffective_reply_reasons(text) == ["admits_no_real_work"]
