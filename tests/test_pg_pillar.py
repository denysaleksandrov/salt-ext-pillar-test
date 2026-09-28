from unittest import mock

import psycopg2
import pytest

from conftest import load_module

QUERY = "SELECT data FROM pillar_data WHERE target IN ('default', %(minion_id)s)"


def make_conn(results):
    """Fake psycopg2 connection; `results` is a list of row lists, one per execute()."""
    conn = mock.MagicMock()
    conn.closed = 0
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = list(results)
    return conn, cursor


@pytest.fixture
def opts(tmp_path):
    pw_file = tmp_path / "pw"
    pw_file.write_text("s3cret\n")
    return {
        "pg_pillar": {
            "profiles": {
                "main": {
                    "host": "db",
                    "dbname": "salt",
                    "user": "salt_pillar",
                    "password_file": str(pw_file),
                    "sslmode": "verify-full",
                    "statement_timeout_ms": 1500,
                }
            }
        }
    }


@pytest.fixture
def pg(opts):
    return load_module("pg_pillar", opts)


def test_rows_merged_in_order(pg):
    rows = [
        ({"ntp": {"servers": ["a"]}, "env": "prod", "users": {"admin": {}}},),
        ({"env": "dev", "users": {"alice": {}}},),
        ('{"ntp": {"servers": ["b"]}}',),  # json text is decoded too
    ]
    conn, cursor = make_conn([rows])
    with mock.patch.object(pg.psycopg2, "connect", return_value=conn):
        ret = pg.ext_pillar("minion1", {}, queries=[QUERY])

    assert ret == {
        "ntp": {"servers": ["b"]},
        "env": "dev",
        "users": {"admin": {}, "alice": {}},
    }
    cursor.execute.assert_called_once_with(QUERY, {"minion_id": "minion1"})


def test_minion_id_is_bound_not_interpolated(pg):
    evil = "x'); DROP TABLE pillar_data; --"
    conn, cursor = make_conn([[]])
    with mock.patch.object(pg.psycopg2, "connect", return_value=conn):
        pg.ext_pillar(evil, {}, queries=QUERY)
    query, params = cursor.execute.call_args.args
    assert evil not in query
    assert params == {"minion_id": evil}


def test_connect_options(pg):
    conn, _ = make_conn([[]])
    with mock.patch.object(pg.psycopg2, "connect", return_value=conn) as connect:
        pg.ext_pillar("m", {}, queries=[QUERY])
    kwargs = connect.call_args.kwargs
    assert kwargs["password"] == "s3cret"
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["options"] == "-c statement_timeout=1500"
    assert "password_file" not in kwargs and "statement_timeout_ms" not in kwargs
    conn.set_session.assert_called_once_with(readonly=True, autocommit=True)


def test_password_env(pg, monkeypatch):
    profile = pg.__opts__["pg_pillar"]["profiles"]["main"]
    del profile["password_file"]
    profile["password_env"] = "PG_TEST_PW"
    monkeypatch.setenv("PG_TEST_PW", "fromenv")
    assert pg._profile_options("main")["password"] == "fromenv"


def test_connection_reused_across_module_reloads(opts):
    conn, _ = make_conn([[], []])
    first = load_module("pg_pillar", opts)
    second = load_module("pg_pillar", opts)  # the Salt loader re-executes modules
    with mock.patch("psycopg2.connect", return_value=conn) as connect:
        first.ext_pillar("m", {}, queries=[QUERY])
        second.ext_pillar("m", {}, queries=[QUERY])
    assert connect.call_count == 1


def test_reconnects_once_on_stale_connection(pg):
    stale, stale_cursor = make_conn([])
    stale_cursor.execute.side_effect = psycopg2.OperationalError("server closed")
    fresh, _ = make_conn([[({"a": 1},)]])
    with mock.patch.object(pg.psycopg2, "connect", side_effect=[stale, fresh]):
        assert pg.ext_pillar("m", {}, queries=[QUERY]) == {"a": 1}
    stale.close.assert_called_once()


def test_fail_closed_raises(pg):
    with mock.patch.object(
        pg.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")
    ):
        with pytest.raises(pg.PgPillarError, match="down"):
            pg.ext_pillar("m", {}, queries=[QUERY])


def test_fail_open_returns_empty(pg):
    with mock.patch.object(
        pg.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")
    ):
        assert pg.ext_pillar("m", {}, queries=[QUERY], fail_closed=False) == {}


def test_non_object_row_rejected(pg):
    conn, _ = make_conn([[(["not", "a", "dict"],)]])
    with mock.patch.object(pg.psycopg2, "connect", return_value=conn):
        with pytest.raises(pg.PgPillarError, match="JSON objects"):
            pg.ext_pillar("m", {}, queries=[QUERY])


def test_namespace_and_null_rows(pg):
    conn, _ = make_conn([[(None,), ({"k": "v"},)]])
    with mock.patch.object(pg.psycopg2, "connect", return_value=conn):
        assert pg.ext_pillar("m", {}, queries=[QUERY], namespace="db") == {
            "db": {"k": "v"}
        }


def test_unknown_profile(pg):
    with pytest.raises(pg.PgPillarError, match="not configured"):
        pg.ext_pillar("m", {}, queries=[QUERY], profile="nope")
