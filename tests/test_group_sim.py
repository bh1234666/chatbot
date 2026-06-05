from group_sim.run_group_sim import is_explicit_bot_mention, latency_stats, parse_ts, unfinished_tasks


def test_group_sim_only_leading_bot_marker_is_explicit_mention():
    assert is_explicit_bot_mention("@bot please help")
    assert is_explicit_bot_mention("  @BOT please help")
    assert not is_explicit_bot_mention("we discussed @bot earlier")
    assert not is_explicit_bot_mention("quote: '@bot please help'")


def test_group_sim_can_strip_suppressed_bot_marker():
    text = "@bot 帮我整理刚才的讨论"
    stripped = text.lstrip()[4:].lstrip(" :：,，")

    assert stripped == "帮我整理刚才的讨论"
    assert not is_explicit_bot_mention(stripped)


def test_unfinished_tasks_ignores_completed_tasks():
    class DummyTask:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    pending = DummyTask(False)
    assert unfinished_tasks([DummyTask(True), pending]) == [pending]


def test_group_sim_latency_stats_include_tail_percentiles():
    stats = latency_stats([{"latency_sec": i} for i in range(1, 101)])

    assert stats["count"] == 100
    assert stats["median"] == 50.5
    assert stats["p90"] == 91.0
    assert stats["p95"] == 96.0
    assert stats["p99"] == 100.0


def test_group_sim_parse_ts_tolerates_bad_values():
    assert parse_ts("2026-05-26T05:03:00") is not None
    assert parse_ts("") is None
    assert parse_ts("not-a-date") is None
