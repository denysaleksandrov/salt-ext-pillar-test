import importlib.util
import json
import pathlib
import threading
from unittest import mock

import psycopg2
import pytest
import requests

from conftest import load_module

API_PATH = pathlib.Path(__file__).resolve().parent.parent / "api" / "pillar_api.py"
spec = importlib.util.spec_from_file_location("pillar_api", API_PATH)
pillar_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pillar_api)

DATA = {
    "default": {"dc": "ams1", "app": {"replicas": 2, "tags": ["a"]}},
    "minion1": {"app": {"version": "1.4.2", "tags": ["b"]}},
}


@pytest.fixture
def data_file(tmp_path):
    path = tmp_path / "pillars.json"
    path.write_text(json.dumps(DATA))
    return path


@pytest.fixture
def serve():
    servers = []

    def _serve(backend, token=None):
        server = pillar_api.make_server(backend, "127.0.0.1", 0, token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield _serve
    for server in servers:
        server.shutdown()
        server.server_close()


def test_deep_merge():
    assert pillar_api.deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}, "d": 3}) == {
        "a": {"b": 1, "c": [2]},
        "d": 3,
    }


def test_dict_backend_merges_default(serve, data_file):
    base = serve(pillar_api.DictBackend(str(data_file)))
    resp = requests.get(f"{base}/pillar/minion1", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"dc": "ams1", "app": {"replicas": 2, "version": "1.4.2", "tags": ["b"]}}
    # Unknown minions still get the defaults.
    assert requests.get(f"{base}/pillar/other", timeout=5).json() == DATA["default"]


def test_dict_backend_404_without_default(serve, tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"minion1": {"a": 1}}))
    base = serve(pillar_api.DictBackend(str(path)))
    assert requests.get(f"{base}/pillar/nobody", timeout=5).status_code == 404


def test_dict_backend_yaml_and_reload(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("minion1:\n  a: 1\n")
    backend = pillar_api.DictBackend(str(path))
    assert backend.get("minion1") == {"a": 1}
    path.write_text("minion1:\n  a: 2\n")
    backend._mtime = None  # mtime granularity can hide a same-second rewrite
    assert backend.get("minion1") == {"a": 2}
    path.write_text("minion1: [broken")  # bad edit keeps the last good data
    backend._mtime = None
    assert backend.get("minion1") == {"a": 2}


def test_dict_backend_rejects_bad_shape(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"minion1": [1, 2]}))
    with pytest.raises(ValueError):
        pillar_api.DictBackend(str(path))


def test_token_auth_and_healthz(serve, data_file):
    base = serve(pillar_api.DictBackend(str(data_file)), token="s3cret")
    assert requests.get(f"{base}/pillar/minion1", timeout=5).status_code == 401
    ok = requests.get(
        f"{base}/pillar/minion1", headers={"Authorization": "Bearer s3cret"}, timeout=5
    )
    assert ok.status_code == 200
    assert requests.get(f"{base}/healthz", timeout=5).json() == {"status": "ok"}


def test_post_lookup(serve, data_file):
    base = serve(pillar_api.DictBackend(str(data_file)))
    resp = requests.post(f"{base}/lookup", json={"id": "minion1"}, timeout=5)
    assert resp.json()["app"]["version"] == "1.4.2"
    assert requests.post(f"{base}/lookup", data="nope", timeout=5).status_code == 400
    assert requests.post(f"{base}/lookup", json={}, timeout=5).status_code == 400


def test_backend_failure_is_503(serve):
    backend = mock.Mock()
    backend.get.side_effect = RuntimeError("db down")
    backend.health.side_effect = RuntimeError("db down")
    base = serve(backend)
    assert requests.get(f"{base}/pillar/m", timeout=5).status_code == 503
    assert requests.get(f"{base}/healthz", timeout=5).status_code == 503


def test_http_pillar_against_server(serve, data_file, tmp_path):
    """The ext_pillar module end to end against the real server (in-process)."""
    base = serve(pillar_api.DictBackend(str(data_file)), token="tok")
    token_file = tmp_path / "token"
    token_file.write_text("tok")
    http = load_module("http_pillar", {})

    ret = http.ext_pillar(
        "minion1", {}, url=base + "/pillar/{minion_id}",
        namespace="api", bearer_token_file=str(token_file),
    )
    assert ret["api"]["app"]["version"] == "1.4.2"

    ret = http.ext_pillar(
        "minion1", {}, url=base + "/lookup", method="POST",
        json_body={"id": "{minion_id}"}, bearer_token_file=str(token_file),
    )
    assert ret["dc"] == "ams1"

    with pytest.raises(http.HttpPillarError, match="401"):
        http.ext_pillar("minion1", {}, url=base + "/pillar/{minion_id}")


def make_pg_backend(rows=None, error=None):
    conn = mock.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    if error:
        cursor.execute.side_effect = error
    pool = mock.Mock()
    pool.getconn.return_value = conn
    with mock.patch("psycopg2.pool.ThreadedConnectionPool", return_value=pool):
        backend = pillar_api.PostgresBackend("dbname=salt")
    return backend, pool, cursor


def test_postgres_backend_merges_rows():
    rows = [({"dc": "ams1", "app": {"replicas": 2}},), ('{"app": {"version": "1"}}',)]
    backend, pool, cursor = make_pg_backend(rows)
    assert backend.get("minion1") == {"dc": "ams1", "app": {"replicas": 2, "version": "1"}}
    query, params = cursor.execute.call_args.args
    assert params == {"default": "default", "minion_id": "minion1"}
    assert "minion1" not in query
    pool.putconn.assert_called_once_with(mock.ANY, close=False)


def test_postgres_backend_no_rows_is_none():
    backend, _, _ = make_pg_backend([])
    assert backend.get("nobody") is None


def test_postgres_backend_discards_broken_connection():
    backend, pool, _ = make_pg_backend(error=psycopg2.OperationalError("gone"))
    with pytest.raises(psycopg2.OperationalError):
        backend.get("m")
    pool.putconn.assert_called_once_with(mock.ANY, close=True)
