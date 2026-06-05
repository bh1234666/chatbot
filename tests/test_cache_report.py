from __future__ import annotations

import json

from app.core.cache_report import (
    evaluate_hash_chain_stability_gate,
    evaluate_hit_rate_gate,
    evaluate_prefix_hash_chain_stability_gate,
    evaluate_shape_coverage_gate,
    evaluate_warm_hit_rate_gate,
    load_cache_gate_baseline,
    load_hash_chain_stability_gate_baseline,
    load_long_helper_prompt_threshold,
    load_prefix_hash_chain_stability_gate_baseline,
    load_reference_hit_rate_baseline,
    load_reference_shape_coverage_baseline,
    load_reference_warm_hit_rate_baseline,
    load_shape_coverage_gate_baseline,
    load_warm_cache_gate_baseline,
    parse_debug_log_text,
    render_cache_report_markdown,
)


SAMPLE_LOG = """\
# Debug log started at 2026-06-02T19:09:48
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=deepseek-main msgs=2 tools=22 static=31200 dynamic=1800 cacheable_prefix=31200 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   {
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     "system_sections": [
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system:## Context And Safety Contract", "bytes": 1000, "hash": "s-static"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system:## Shared Files", "bytes": 120, "hash": "files-a"}
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ],
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     "message_sections": [
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "msg1.user:## Conversation History", "bytes": 900, "hash": "hist-stable"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "msg1.user:## Current Message To Answer", "bytes": 40, "hash": "msg-a"}
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ]
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ,"hash_chain": [
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 1000, "hash": "chain-system", "segment_hash": "seg-system"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 300, "hash": "chain-tools", "segment_hash": "seg-tools"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 120, "hash": "chain-dynamic", "segment_hash": "seg-dynamic"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 900, "hash": "chain-msg-a", "segment_hash": "seg-msg-a"}
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ]
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   }
[19:10:02.001] [trace-a             ] [llm.cache_stats] P49 [main]: model=deepseek-main prompt=5000 completion=300 cache_hit=4200 cache_miss=800 hit_rate=84%
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=deepseek-main msgs=2 tools=22 static=31200 dynamic=2100 cacheable_prefix=31200 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]   {
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     "system_sections": [
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system:## Context And Safety Contract", "bytes": 1000, "hash": "s-static"},
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system:## Shared Files", "bytes": 120, "hash": "files-a"}
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     ],
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     "message_sections": [
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "msg1.user:## Conversation History", "bytes": 900, "hash": "hist-stable"},
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "msg1.user:## Current Message To Answer", "bytes": 60, "hash": "msg-b"}
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     ]
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     ,"hash_chain": [
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 1000, "hash": "chain-system", "segment_hash": "seg-system"},
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 300, "hash": "chain-tools", "segment_hash": "seg-tools"},
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 120, "hash": "chain-dynamic", "segment_hash": "seg-dynamic"},
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 920, "hash": "chain-msg-b", "segment_hash": "seg-msg-b"}
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]     ]
[19:10:03.001] [trace-b             ] [llm.prompt_cache_shape]   }
[19:10:05.001] [trace-b             ] [llm.cache_stats] P49 [main]: model=deepseek-main prompt=6000 completion=350 cache_hit=5400 cache_miss=600 hit_rate=90%
[19:10:07.001] [trace-b.read        ] [llm.cache_stats] P49 [helper.read_ielts]: model=deepseek-main prompt=3000 completion=200 cache_hit=2100 cache_miss=900 hit_rate=70%
[19:10:12.001] [trace-b             ] [llm.cache_stats] P49 [main]: model=deepseek-main prompt=4000 completion=100 cache_hit=1600 cache_miss=2400 hit_rate=40%
[19:10:14.001] [trace-route         ] [llm.cache_stats] P49 [main]: model=deepseek-pro prompt=3000 completion=100 cache_hit=2400 cache_miss=600 hit_rate=80%
[19:10:15.001] [trace-route         ] [round2.upgrade_hard] upgrading from medium to hard
[19:10:16.001] [trace-route         ] [llm.cache_stats] P49 [main]: model=deepseek-flash prompt=3000 completion=100 cache_hit=300 cache_miss=2700 hit_rate=10%
[19:10:20.001] [trace-helper        ] [delegate.helper_route] task_id=x kind=code mode=easy resume=False resumed_actually=False helper_lite=True helper_think=False fail_count=0 model=deepseek-flash reasoning=disabled
[19:10:21.001] [trace-helper        ] [llm.cache_stats] P49 [helper.x]: model=deepseek-flash prompt=3000 completion=100 cache_hit=2500 cache_miss=500 hit_rate=83%
[19:10:22.001] [trace-helper        ] [delegate.helper_route] task_id=x kind=code mode=hard resume=True resumed_actually=True helper_lite=True helper_think=False fail_count=1 model=deepseek-pro reasoning=max
[19:10:23.001] [trace-helper        ] [llm.cache_stats] P49 [helper.x]: model=deepseek-pro prompt=3000 completion=100 cache_hit=400 cache_miss=2600 hit_rate=13%
[19:10:30.001] [trace-start         ] [llm.tools.start] model=deepseek-flash reasoning=disabled tools=17 max_iter=None
[19:10:31.001] [trace-start         ] [llm.cache_stats] P49 [helper.start]: model=deepseek-flash prompt=2000 completion=50 cache_hit=1800 cache_miss=200 hit_rate=90%
[19:10:32.001] [trace-start         ] [llm.tools.start] model=deepseek-pro reasoning=max tools=17 max_iter=None
[19:10:33.001] [trace-start         ] [llm.cache_stats] P49 [helper.start]: model=deepseek-pro prompt=2000 completion=50 cache_hit=200 cache_miss=1800 hit_rate=10%
[19:11:00.001] [trace-first         ] [llm.cache_stats] P49 [helper.first_seen]: model=deepseek-flash prompt=2000 completion=50 cache_hit=200 cache_miss=1800 hit_rate=10%
[19:11:10.001] [trace-long-a        ] [llm.cache_stats] P49 [helper.long_idle]: model=deepseek-flash prompt=2000 completion=50 cache_hit=1800 cache_miss=200 hit_rate=90%
[19:17:00.001] [trace-long-b        ] [llm.cache_stats] P49 [helper.long_idle]: model=deepseek-flash prompt=2000 completion=50 cache_hit=200 cache_miss=1800 hit_rate=10%
[19:17:05.001] [trace-short-a       ] [llm.cache_stats] P49 [main.short]: model=deepseek-main prompt=2000 completion=50 cache_hit=1800 cache_miss=200 hit_rate=90%
[19:17:07.001] [trace-short-b       ] [llm.cache_stats] P49 [main.short]: model=deepseek-main prompt=2000 completion=50 cache_hit=200 cache_miss=1800 hit_rate=10%
[19:17:10.001] [trace-route-low     ] [llm.cache_stats] P49 [main.route_low]: model=deepseek-main prompt=2000 completion=50 cache_hit=1800 cache_miss=200 hit_rate=90%
[19:17:11.001] [trace-route-low     ] [llm.tools.model_switch] from=deepseek-main to=deepseek-pro reason=hard
[19:17:12.001] [trace-route-low     ] [llm.cache_stats] P49 [main.route_low]: model=deepseek-pro prompt=2000 completion=50 cache_hit=200 cache_miss=1800 hit_rate=10%
"""


def test_parse_debug_log_text_extracts_shape_and_usage() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)

    assert len(report.shapes) == 2
    assert len(report.stats) == 17
    assert report.shapes[0].label == "tools_loop.iter1.main"
    assert report.shapes[0].tag_hint == "main"
    assert report.shapes[0].cacheable_prefix_bytes == 31200
    assert report.shapes[0].system_sections[0]["label"] == "system:## Context And Safety Contract"
    assert report.shapes[0].message_sections[1]["hash"] == "msg-a"
    assert report.shapes[0].hash_chain[0]["label"] == "system_static"
    assert report.shapes[0].hash_chain[-1]["segment_hash"] == "seg-msg-a"
    assert report.stats[0].tag == "main"
    assert report.stats[0].cache_hit_tokens == 4200
    assert report.stats[0].timestamp == 69002.001
    assert report.route_events[0].category == "round2.upgrade_hard"


def test_render_cache_report_markdown_groups_rows() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)
    markdown = render_cache_report_markdown(report)

    assert "# Prompt Cache Report" in markdown
    assert "local prefix share" in markdown
    assert "| tools_loop.iter1.main | main | deepseek-main | 2 | 31200 | 1950 | 31200 | 94.1% | 1 | 1 |" in markdown
    assert "## Section Stability" in markdown
    assert "| tools_loop.iter1.main | deepseek-main | message | msg1.user:## Current Message To Answer | 2 | 2 | 50 |" in markdown
    assert "## Hash Chain Stability" in markdown
    assert "| tools_loop.iter1.main | deepseek-main | messages | 2 | 2 | 2 | 910 |" in markdown
    assert "| tools_loop.iter1.main | deepseek-main | system_static | 2 |" not in markdown
    assert "## Prefix Hash Chain Stability" in markdown
    assert "| (all prefix-critical segments stable) |  |  | 0 | 0 | 0 | 0 |" in markdown
    assert "## Shape Coverage" in markdown
    assert "| main | deepseek-main | 3 | 3 | 100.0% |" in markdown
    assert "| helper.first_seen | deepseek-flash | 1 | 0 | 0.0% |" in markdown
    assert "| main | deepseek-main | 3 | 15000 | 750 | 11200 | 3800 | 74.7% |" in markdown
    assert "## Provider Cache Usage By Tag" in markdown
    assert "| main | deepseek-flash,deepseek-main,deepseek-pro | 5 | 21000 | 950 | 13900 | 7100 | 66.2% |" in markdown
    assert "## Model Route Diagnostics" in markdown
    assert "| trace-route | main | 2 | 1 | deepseek-pro -> deepseek-flash | upgrade:hard | explained | 45.0% | 3300 |" in markdown
    assert "| trace-helper | helper.x | 2 | 1 | deepseek-flash -> deepseek-pro | helper_route, helper_route | explained | 48.3% | 3100 |" in markdown
    assert "| trace-start | helper.start | 2 | 1 | deepseek-flash -> deepseek-pro | tools_start, tools_start | explained | 50.0% | 2000 |" in markdown
    assert "## Low-Hit Cause Summary" in markdown
    assert "| short_interval_upstream_or_ttl | main | deepseek-main | 1 | 4000 | 1600 | 2400 | 40.0% |" in markdown
    assert "| first_seen_tag_model | helper.first_seen | deepseek-flash | 1 | 2000 | 200 | 1800 | 10.0% |" in markdown
    assert "## Low-Hit Call Diagnostics" in markdown
    assert "| 19:10:12.001 | trace-b | main | deepseek-main | 40.0% | 1600 | 2400 | 7.0 | short_interval_upstream_or_ttl | none | shape_seen:tools_loop.iter1.main |" in markdown
    assert "| 19:11:00.001 | trace-first | helper.first_seen | deepseek-flash | 10.0% | 200 | 1800 | n/a | first_seen_tag_model | none | shape_missing |" in markdown
    assert "| 19:17:00.001 | trace-long-b | helper.long_idle | deepseek-flash | 10.0% | 200 | 1800 | 350.0 | ttl_or_long_idle | none | shape_missing |" in markdown
    assert "| 19:17:07.001 | trace-short-b | main.short | deepseek-main | 10.0% | 200 | 1800 | 2.0 | short_interval_prefix_change | none | shape_missing |" in markdown
    assert "| 19:17:12.001 | trace-route-low | main.route_low | deepseek-pro | 10.0% | 200 | 1800 | n/a | model_switch_cold_start | model_switch | shape_missing |" in markdown
    assert "system/tool hash" in markdown
    assert "tag|model" in markdown


def test_evaluate_hit_rate_gate() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)

    assert evaluate_hit_rate_gate(report, minimum_by_tag={"main": 60}) == []
    assert evaluate_hit_rate_gate(report, minimum_by_tag={"helper.*": 40}) == []
    assert evaluate_hit_rate_gate(
        report,
        minimum_by_tag={"long_helper": 40},
        long_helper_min_prompt_tokens=2500,
    ) == []
    assert evaluate_hit_rate_gate(report, minimum_by_tag={"main": 90}) == [
        "main hit_rate 61.7% < 90.0%"
    ]
    assert evaluate_hit_rate_gate(report, minimum_by_tag={"helper.*": 90}) == [
        "helper.* hit_rate 48.4% < 90.0%"
    ]
    assert evaluate_hit_rate_gate(
        report,
        minimum_by_tag={"long_helper": 90},
        long_helper_min_prompt_tokens=2500,
    ) == [
        "long_helper hit_rate 55.6% < 90.0%"
    ]
    assert evaluate_hit_rate_gate(report, minimum_by_tag={"main|deepseek-main": 90}) == [
        "main|deepseek-main hit_rate 71.4% < 90.0%"
    ]
    assert evaluate_hit_rate_gate(report, minimum_by_tag={"missing": 10}) == [
        "missing cache stats for missing"
    ]


def test_evaluate_warm_hit_rate_gate_skips_first_same_tag_model_calls() -> None:
    log = """\
[19:00:00.000] [trace-a             ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=1000 completion=1 cache_hit=200 cache_miss=800 hit_rate=20%
[19:00:01.000] [trace-a             ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=1000 completion=1 cache_hit=700 cache_miss=300 hit_rate=70%
[19:00:02.000] [trace-a             ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=1000 completion=1 cache_hit=980 cache_miss=20 hit_rate=98%
[19:00:03.000] [trace-a             ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=1000 completion=1 cache_hit=970 cache_miss=30 hit_rate=97%
"""
    report = parse_debug_log_text(log)

    assert evaluate_hit_rate_gate(report, minimum_by_tag={"helper.*": 95}) == [
        "helper.* hit_rate 71.2% < 95.0%"
    ]
    assert evaluate_warm_hit_rate_gate(
        report,
        minimum_by_tag={"helper.*": 95},
        skip_first=2,
    ) == []
    assert evaluate_warm_hit_rate_gate(report, minimum_by_tag={"main": 90}) == [
        "missing warm cache stats for main"
    ]


def test_short_interval_low_hit_uses_hash_chain_when_shape_changes() -> None:
    log = """\
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]   {
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]     "hash_chain": [
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 10, "hash": "s", "segment_hash": "s"},
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 10, "hash": "t", "segment_hash": "t"},
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 10, "hash": "d", "segment_hash": "d"},
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "m1", "segment_hash": "m1"}
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]     ]
[19:00:00.000] [trace-one           ] [llm.prompt_cache_shape]   }
[19:00:01.000] [trace-one           ] [llm.cache_stats] P49 [main]: model=m prompt=100 completion=1 cache_hit=90 cache_miss=10 hit_rate=90%
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=20 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]   {
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]     "hash_chain": [
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 10, "hash": "s", "segment_hash": "s"},
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 10, "hash": "t", "segment_hash": "t"},
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 10, "hash": "d", "segment_hash": "d"},
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 20, "hash": "m2", "segment_hash": "m2"}
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]     ]
[19:00:02.000] [trace-two           ] [llm.prompt_cache_shape]   }
[19:00:03.000] [trace-two           ] [llm.cache_stats] P49 [main]: model=m prompt=100 completion=1 cache_hit=10 cache_miss=90 hit_rate=10%
"""
    markdown = render_cache_report_markdown(parse_debug_log_text(log))

    assert "| short_interval_messages_change | main | m | 1 | 100 | 10 | 90 | 10.0% |" in markdown
    assert "| 19:00:03.000 | trace-two | main | m | 10.0% | 10 | 90 | 2.0 | short_interval_messages_change | none | shape_seen:tools_loop.iter1.main |" in markdown


def test_helper_shape_hint_matches_specific_helper_task_only() -> None:
    log = """\
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape] tools_loop.iter1.helper.read_docs: model=m msgs=2 tools=4 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]   {
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]     "hash_chain": [
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 10, "hash": "s", "segment_hash": "s"},
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 10, "hash": "t", "segment_hash": "t"},
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 10, "hash": "d", "segment_hash": "d"},
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "m1", "segment_hash": "m1"}
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]     ]
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape]   }
[19:00:01.000] [trace-helper        ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=100 completion=1 cache_hit=20 cache_miss=80 hit_rate=20%
[19:00:02.000] [trace-helper        ] [llm.cache_stats] P49 [helper.other]: model=m prompt=100 completion=1 cache_hit=10 cache_miss=90 hit_rate=10%
"""
    report = parse_debug_log_text(log)
    markdown = render_cache_report_markdown(report)

    assert report.shapes[0].tag_hint == "helper.read_docs"
    assert "| helper.read_docs | m | 1 | 1 | 100.0% |" in markdown
    assert "| helper.other | m | 1 | 0 | 0.0% |" in markdown
    assert "| 19:00:02.000 | trace-helper | helper.other | m | 10.0% | 10 | 90 | n/a | first_seen_tag_model | none | shape_missing |" in markdown
    assert "shape_seen:tools_loop.iter1.helper.read_docs" in markdown


def test_helper_kind_usage_reuses_concrete_helper_shape_evidence() -> None:
    log = """\
[19:00:00.000] [trace-helper        ] [llm.prompt_cache_shape] tools_loop.iter1.helper.read_docs: model=m msgs=2 tools=4 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:01.000] [trace-helper        ] [llm.cache_stats] P49 [helper.read_docs]: model=m prompt=100 completion=1 cache_hit=90 cache_miss=10 hit_rate=90%
[19:00:01.000] [trace-helper        ] [llm.cache_stats] P49 [helper_kind.read]: model=m prompt=100 completion=1 cache_hit=90 cache_miss=10 hit_rate=90%
"""
    report = parse_debug_log_text(log)
    markdown = render_cache_report_markdown(report)

    assert "| helper.read_docs | m | 1 | 1 | 100.0% |" in markdown
    assert "| helper_kind.read | m | 1 | 1 | 100.0% |" in markdown
    assert evaluate_shape_coverage_gate(
        report,
        minimum_by_tag={"helper_kind.read": 100, "helper.*": 100},
    ) == []


def test_tools_loop_nonstream_suffix_shape_matches_usage_tag() -> None:
    log = """\
[19:00:00.000] [trace-main          ] [llm.prompt_cache_shape] tools_loop.iter3.main.no_tools_fallback: model=m msgs=2 tools=0 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:01.000] [trace-main          ] [llm.cache_stats] P49 [main.no_tools_fallback]: model=m prompt=100 completion=1 cache_hit=90 cache_miss=10 hit_rate=90%
[19:00:02.000] [trace-helper        ] [llm.prompt_cache_shape] tools_loop.iter4.helper.read_docs.call.final_cleanup: model=m msgs=2 tools=0 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:03.000] [trace-helper        ] [llm.cache_stats] P49 [helper.read_docs.final_cleanup]: model=m prompt=100 completion=1 cache_hit=90 cache_miss=10 hit_rate=90%
"""
    report = parse_debug_log_text(log)

    assert report.shapes[0].tag_hint == "main"
    assert report.shapes[1].tag_hint == "helper.read_docs"
    assert evaluate_shape_coverage_gate(
        report,
        minimum_by_tag={"main": 100, "helper.*": 100},
    ) == []


def test_chat_stream_shape_matches_chat_stream_usage_tag() -> None:
    log = """\
[19:00:00.000] [trace-round3        ] [llm.prompt_cache_shape] chat_stream: model=m msgs=2 tools=0 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:00:01.000] [trace-round3        ] [llm.cache_stats] P49 [chat_stream]: model=m prompt=100 completion=10 cache_hit=90 cache_miss=10 hit_rate=90%
"""
    report = parse_debug_log_text(log)

    assert report.shapes[0].tag_hint == "chat_stream"
    assert evaluate_shape_coverage_gate(
        report,
        minimum_by_tag={"chat_stream": 100},
    ) == []


def test_evaluate_shape_coverage_gate() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)

    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"main": 30}) == []
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"helper.*": 0}) == []
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"main": 90}) == [
        "main shape_coverage 33.3% < 90.0%"
    ]
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"helper.*": 1}) == [
        "helper.* shape_coverage 0.0% < 1.0%"
    ]
    assert evaluate_shape_coverage_gate(
        report,
        minimum_by_tag={"long_helper": 1},
        long_helper_min_prompt_tokens=2500,
    ) == [
        "long_helper shape_coverage 0.0% < 1.0%"
    ]
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"helper.first_seen": 1}) == [
        "helper.first_seen shape_coverage 0.0% < 1.0%"
    ]
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"missing": 10}) == [
        "missing shape coverage stats for missing"
    ]


def test_evaluate_hash_chain_stability_gate() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)

    assert evaluate_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"tools_loop.iter1.main": 1},
    ) == []
    assert evaluate_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"tools_loop.iter1.main": 0},
    ) == [
        "tools_loop.iter1.main unstable_hash_chain_groups 1 > 0"
    ]
    assert evaluate_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"missing": 0},
    ) == []


def test_evaluate_prefix_hash_chain_stability_gate_ignores_dynamic_messages() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)

    assert evaluate_prefix_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"tools_loop.iter1.main": 0},
    ) == []
    assert evaluate_prefix_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"tools_loop.iter*.main": 0},
    ) == []


def test_evaluate_prefix_hash_chain_stability_gate_catches_prefix_changes() -> None:
    log = """\
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   {
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     "hash_chain": [
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 100, "hash": "sys-a", "segment_hash": "sys-a"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 30, "hash": "tool-a", "segment_hash": "tool-a"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 0, "hash": "dyn", "segment_hash": "dyn"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "msg-a", "segment_hash": "msg-a"}
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ]
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   }
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=cccccccccccccccc tools_hash=bbbbbbbbbbbbbbbb
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]   {
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]     "hash_chain": [
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 100, "hash": "sys-b", "segment_hash": "sys-b"},
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 30, "hash": "tool-b", "segment_hash": "tool-a"},
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 0, "hash": "dyn", "segment_hash": "dyn"},
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "msg-b", "segment_hash": "msg-b"}
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]     ]
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]   }
"""
    report = parse_debug_log_text(log)

    assert evaluate_prefix_hash_chain_stability_gate(
        report,
        maximum_unstable_by_label={"tools_loop.iter1.main": 0},
    ) == [
        "tools_loop.iter1.main unstable_prefix_hash_chain_groups 2 > 0"
    ]


def test_load_cache_gate_baseline(tmp_path) -> None:
    path = tmp_path / "cache_baseline.json"
    path.write_text(
        '{"minimum_hit_rate_by_tag":{"main":85,"helper_kind.read":70},'
        '"minimum_warm_hit_rate_by_tag":{"main":99,"helper.*":98},'
        '"minimum_shape_coverage_by_tag":{"main":95},'
        '"long_helper_min_prompt_tokens":25000,'
        '"maximum_unstable_hash_chain_groups_by_label":{"tools_loop.iter1.main":0},'
        '"maximum_unstable_prefix_hash_chain_groups_by_label":{"tools_loop.iter*.main":0}}',
        encoding="utf-8",
    )

    assert load_cache_gate_baseline(path) == {"main": 85.0, "helper_kind.read": 70.0}
    assert load_warm_cache_gate_baseline(path) == {"main": 99.0, "helper.*": 98.0}
    assert load_shape_coverage_gate_baseline(path) == {"main": 95.0}
    assert load_long_helper_prompt_threshold(path) == 25000
    assert load_hash_chain_stability_gate_baseline(path) == {"tools_loop.iter1.main": 0.0}
    assert load_prefix_hash_chain_stability_gate_baseline(path) == {"tools_loop.iter*.main": 0.0}


def test_shape_coverage_gate_baseline_defaults_to_empty(tmp_path) -> None:
    path = tmp_path / "cache_baseline.json"
    path.write_text('{"minimum_hit_rate_by_tag":{"main":85}}', encoding="utf-8")

    assert load_warm_cache_gate_baseline(path) == {}
    assert load_shape_coverage_gate_baseline(path) == {}
    assert load_long_helper_prompt_threshold(path) == 20000
    assert load_hash_chain_stability_gate_baseline(path) == {}
    assert load_prefix_hash_chain_stability_gate_baseline(path) == {}


def test_reference_cache_lines_are_not_loaded_as_hard_gates(tmp_path) -> None:
    path = tmp_path / "cache_baseline.json"
    path.write_text(
        '{"reference_hit_rate_by_tag":{"main":95},'
        '"reference_warm_hit_rate_by_tag":{"main":99,"helper.*":99},'
        '"reference_shape_coverage_by_tag":{"main":99}}',
        encoding="utf-8",
    )

    assert load_cache_gate_baseline(path) == {}
    assert load_warm_cache_gate_baseline(path) == {}
    assert load_shape_coverage_gate_baseline(path) == {}
    assert load_reference_hit_rate_baseline(path) == {"main": 95.0}
    assert load_reference_warm_hit_rate_baseline(path) == {"main": 99.0, "helper.*": 99.0}
    assert load_reference_shape_coverage_baseline(path) == {"main": 99.0}


def test_render_cache_report_markdown_shows_reference_lines_without_gating() -> None:
    report = parse_debug_log_text(SAMPLE_LOG)
    markdown = render_cache_report_markdown(
        report,
        reference_hit_rate_by_tag={"main": 95, "helper.*": 96},
        reference_warm_hit_rate_by_tag={"main": 99, "helper.*": 99},
        reference_shape_coverage_by_tag={"main": 99},
        long_helper_min_prompt_tokens=2500,
    )

    assert "## Reference Convergence Lines" in markdown
    assert "| scope | reference | observed | delta | calls | status |" in markdown
    assert "| target | reference | observed | delta | calls | status |" not in markdown
    assert "### Global Hit-Rate References" in markdown
    assert "| main | 95.0% | 61.7% | -33.3% | 9 | below_reference |" in markdown
    assert "| helper.* | 96.0% | 48.4% | -47.6% | 8 | below_reference |" in markdown
    assert "### Warm Hit-Rate References" in markdown
    assert "| main | 99.0% | 40.0% | -59.0% | 1 | below_reference |" in markdown
    assert "| helper.* | 99.0% | n/a | n/a | 0 | missing |" in markdown
    assert "### Shape Coverage References" in markdown
    assert "| main | 99.0% | 33.3% | -65.7% | 9 | below_reference |" in markdown


def test_cache_report_cli_expands_globs(tmp_path, monkeypatch) -> None:
    from scripts.cache_report import _expand_log_paths

    one = tmp_path / "debug_a.log"
    two = tmp_path / "debug_b.log"
    one.write_text("", encoding="utf-8")
    two.write_text("", encoding="utf-8")

    pattern = str(tmp_path / "debug_*.log")
    expanded = _expand_log_paths([pattern])

    assert expanded == sorted([str(one), str(two)])


def test_cache_report_cli_shape_coverage_gate_fails(tmp_path) -> None:
    from scripts.cache_report import main

    log_path = tmp_path / "debug.log"
    out_path = tmp_path / "report.md"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")

    assert main([str(log_path), "-o", str(out_path), "--min-shape-coverage", "main=90"]) == 2
    assert out_path.is_file()


def test_cache_report_cli_baseline_warm_gate_fails(tmp_path) -> None:
    from scripts.cache_report import main

    log_path = tmp_path / "debug.log"
    baseline_path = tmp_path / "cache_baseline.json"
    out_path = tmp_path / "report.md"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    baseline_path.write_text(
        '{"minimum_warm_hit_rate_by_tag":{"main":99}}',
        encoding="utf-8",
    )

    assert main([str(log_path), "-o", str(out_path), "--baseline", str(baseline_path)]) == 2
    assert out_path.is_file()


def test_cache_report_cli_reference_lines_do_not_fail(tmp_path) -> None:
    from scripts.cache_report import main

    log_path = tmp_path / "debug.log"
    baseline_path = tmp_path / "cache_baseline.json"
    out_path = tmp_path / "report.md"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    baseline_path.write_text(
        '{"description":"reference only",'
        '"reference_hit_rate_by_tag":{"main":95},'
        '"reference_warm_hit_rate_by_tag":{"main":99},'
        '"reference_shape_coverage_by_tag":{"main":99}}',
        encoding="utf-8",
    )

    assert main([str(log_path), "-o", str(out_path), "--baseline", str(baseline_path)]) == 0
    assert out_path.is_file()
    markdown = out_path.read_text(encoding="utf-8")
    assert "## Reference Convergence Lines" in markdown
    assert "| main | 99.0% |" in markdown


def test_repository_default_cache_baseline_uses_reference_percentages(tmp_path) -> None:
    from pathlib import Path
    from scripts.cache_report import main

    baseline_path = Path("config/cache_baseline.json")
    raw = baseline_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    description = data.get("description", "")
    assert "reference_hit_rate_by_tag" in raw
    assert "reference_warm_hit_rate_by_tag" in raw
    assert "reference_shape_coverage_by_tag" in raw
    assert "minimum_hit_rate_by_tag" not in raw
    assert "minimum_warm_hit_rate_by_tag" not in raw
    assert "minimum_shape_coverage_by_tag" not in raw
    assert "maximum_unstable_hash_chain_groups_by_label" not in raw
    assert "maximum_unstable_prefix_hash_chain_groups_by_label" in raw
    assert "until representative tests converge" in description
    assert "may be higher or lower than any reference line" in description
    assert "Round2/helper long workflows prioritize stable cacheable prefixes" in description
    assert "Round1/Round3 fast paths prioritize low latency and low token load" in description
    assert "does not make the path noticeably heavier" in description

    log_path = tmp_path / "debug.log"
    out_path = tmp_path / "report.md"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")

    assert main([str(log_path), "-o", str(out_path), "--baseline", str(baseline_path)]) == 0
    markdown = out_path.read_text(encoding="utf-8")
    assert "## Reference Convergence Lines" in markdown
    assert "### Global Hit-Rate References" in markdown
    assert "### Warm Hit-Rate References" in markdown
    assert "### Shape Coverage References" in markdown


def test_repository_default_cache_baseline_accepts_shape_only_logs(tmp_path) -> None:
    from pathlib import Path
    from scripts.cache_report import main

    baseline_path = Path("config/cache_baseline.json")
    log_path = tmp_path / "debug_shape_only.log"
    out_path = tmp_path / "report.md"
    log_path.write_text(
        """\
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]   {
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]     "hash_chain": [
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 100, "hash": "sys-a", "segment_hash": "sys-a"},
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 30, "hash": "tool-a", "segment_hash": "tool-a"},
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]       {"label": "system_dynamic", "bytes": 0, "hash": "dyn", "segment_hash": "dyn"},
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "msg-a", "segment_hash": "msg-a"}
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]     ]
[19:10:00.001] [trace-shape         ] [llm.prompt_cache_shape]   }
""",
        encoding="utf-8",
    )

    assert main([str(log_path), "-o", str(out_path), "--baseline", str(baseline_path)]) == 0
    markdown = out_path.read_text(encoding="utf-8")
    assert "## Provider Usage Evidence Missing" in markdown
    assert "cannot prove the real upstream cache hit rate or convergence plateau" in markdown
    assert "不能证明真实命中率或收敛平台期" in markdown
    assert "## Reference Convergence Lines" in markdown
    assert "| (no usage rows) |  | 0 | 0 | n/a |" in markdown
    assert "| main | 95.0% | n/a | n/a | 0 | missing |" in markdown
    assert "| main | 99.0% | n/a | n/a | 0 | missing |" in markdown


def test_cache_report_cli_hash_chain_gate_fails(tmp_path) -> None:
    from scripts.cache_report import main

    log_path = tmp_path / "debug.log"
    out_path = tmp_path / "report.md"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")

    assert main([
        str(log_path),
        "-o",
        str(out_path),
        "--max-unstable-hash-chain",
        "tools_loop.iter1.main=0",
    ]) == 2
    assert out_path.is_file()


def test_cache_report_cli_prefix_hash_chain_gate_fails(tmp_path) -> None:
    from scripts.cache_report import main

    log_path = tmp_path / "debug.log"
    out_path = tmp_path / "report.md"
    log_path.write_text(
        """\
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=aaaaaaaaaaaaaaaa tools_hash=bbbbbbbbbbbbbbbb
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   {
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     "hash_chain": [
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 100, "hash": "sys-a", "segment_hash": "sys-a"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 30, "hash": "tool-a", "segment_hash": "tool-a"},
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "msg-a", "segment_hash": "msg-a"}
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]     ]
[19:10:00.001] [trace-a             ] [llm.prompt_cache_shape]   }
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape] tools_loop.iter1.main: model=m msgs=2 tools=1 static=100 dynamic=10 cacheable_prefix=100 system=cccccccccccccccc tools_hash=bbbbbbbbbbbbbbbb
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]   {
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]     "hash_chain": [
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "system_static", "bytes": 100, "hash": "sys-b", "segment_hash": "sys-b"},
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "tool_schema", "bytes": 30, "hash": "tool-b", "segment_hash": "tool-a"},
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]       {"label": "messages", "bytes": 10, "hash": "msg-b", "segment_hash": "msg-b"}
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]     ]
[19:10:01.001] [trace-b             ] [llm.prompt_cache_shape]   }
""",
        encoding="utf-8",
    )

    assert main([
        str(log_path),
        "-o",
        str(out_path),
        "--max-unstable-prefix-hash-chain",
        "tools_loop.iter*.main=0",
    ]) == 2
    assert out_path.is_file()
