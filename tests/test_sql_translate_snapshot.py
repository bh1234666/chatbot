import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.pool import _translate_sql


def test_translate_dollar_args_preserves_occurrence_order():
    sql, args = _translate_sql(
        "SELECT * FROM t WHERE a = $2 AND b = $1 AND c = $2",
        ("one", "two"),
    )

    assert sql == "SELECT * FROM t WHERE a = ? AND b = ? AND c = ?"
    assert args == ("two", "one", "two")


def test_translate_any_expands_middle_argument_without_shifting_later_args():
    sql, args = _translate_sql(
        "UPDATE cold_nodes SET access_count = access_count + 1, last_access = NOW() "
        "WHERE archive_id = $1 AND id = ANY($2::text[]) AND group_id = $3",
        ("archive", ["a", "b"], "group"),
    )

    assert "id IN (?,?)" in sql
    assert "last_access = datetime('now')" in sql
    assert args == ("archive", "a", "b", "group")


def test_translate_any_empty_list_uses_null_sentinel():
    sql, args = _translate_sql(
        "DELETE FROM warm_memories WHERE archive_id = $1 AND id = ANY($2::text[])",
        ("archive", []),
    )

    assert "id IN (NULL)" in sql
    assert args == ("archive",)


def test_translate_common_postgres_constructs():
    sql, args = _translate_sql(
        "SELECT GREATEST(score, $1::int), LEAST(score, $2::int) FROM t "
        "WHERE name ILIKE $3 AND created_at < NOW()",
        (1, 9, "%abc%"),
    )

    assert "MAX(score, ?)" in sql
    assert "MIN(score, ?)" in sql
    assert "name LIKE ?" in sql
    assert "datetime(created_at) < datetime('now')" in sql
    assert args == (1, 9, "%abc%")


def test_translate_ignores_literals_and_comments():
    sql, args = _translate_sql(
        "SELECT '$1', '-- $2', col FROM t -- id = ANY($3::text[])\n"
        "WHERE id = $1 /* $2 should stay */",
        ("real", "unused", ["ignored"]),
    )

    assert "'$1'" in sql
    assert "-- id = ANY($3::text[])" in sql
    assert "/* $2 should stay */" in sql
    assert sql.endswith("WHERE id = ? /* $2 should stay */")
    assert args == ("real",)


def test_translate_any_accepts_scalar_value_as_single_item():
    sql, args = _translate_sql(
        "SELECT * FROM t WHERE id = ANY($1::text[]) AND archive_id = $2",
        ("node-1", "archive"),
    )

    assert "id IN (?)" in sql
    assert args == ("node-1", "archive")


def test_translate_missing_dollar_argument_becomes_none():
    sql, args = _translate_sql("SELECT * FROM t WHERE a = $1 AND missing = $3", ("a",))

    assert sql == "SELECT * FROM t WHERE a = ? AND missing = ?"
    assert args == ("a", None)


def test_translate_casts_outside_any_are_removed_without_touching_literals():
    sql, args = _translate_sql(
        "SELECT $1::jsonb, '$2::jsonb' AS literal, $2::bigint[] FROM t",
        ({"k": "v"}, [1, 2]),
    )

    assert sql == "SELECT ?, '$2::jsonb' AS literal, ? FROM t"
    assert args == ({"k": "v"}, [1, 2])
