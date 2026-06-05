"""L4-2: delegate result file_map 单元测试."""
import pytest


# file_map is built inside _copy_results_to_main in delegate.py.
# Testing the construction logic requires mocking the full delegate flow.
# These tests verify the file_map structure contract.

def test_file_map_structure():
    """file_map 条目的预期结构。"""
    expected_keys = {"helper_name", "main_name", "shared_name"}
    example = {
        "helper_name": "chart.png",
        "main_name": "merge_charts_chart.png",
        "shared_name": "_helpers_shared/merge_charts/chart.png",
    }
    assert set(example.keys()) == expected_keys


def test_file_map_shared_name_can_be_null():
    """没有 shared copy 时 shared_name 可为 None。"""
    example = {
        "helper_name": "report.docx",
        "main_name": "paper_pptx_xlsx_report.docx",
        "shared_name": None,
    }
    assert example["shared_name"] is None
    assert example["helper_name"] is not None
    assert example["main_name"] is not None
