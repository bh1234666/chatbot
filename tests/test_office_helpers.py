"""
office_common / office_xlsx 特征测试(纯 helper 部分)。
docx/pptx/xlsx 的读写需 python-docx/pptx/openpyxl,留待本地装好依赖后补;
这里只覆盖不依赖第三方库的纯 helper。可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.office_common import _jsonable, _err
from app.llm.tools.office_xlsx import _openpyxl_col_letter


def test_jsonable_handles_set_and_nested():
    out = _jsonable({1, 2})
    # 可被 json 序列化(set 被转为可序列化结构)
    import json
    json.dumps(_jsonable({"a": {1, 2}, "b": [1, 2, 3]}))


def test_err_returns_error_payload():
    r = _err("something failed")
    # _err 产出结构化错误(dict 或 JSON 串),包含错误信息
    assert r is not None
    text = r if isinstance(r, str) else str(r)
    assert "something failed" in text or "error" in text.lower()


def test_openpyxl_col_letter():
    assert _openpyxl_col_letter(1) == "A"
    assert _openpyxl_col_letter(26) == "Z"
    assert _openpyxl_col_letter(27) == "AA"
    assert _openpyxl_col_letter(28) == "AB"
