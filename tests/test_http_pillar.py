from unittest import mock

import pytest
import requests

from conftest import load_module

URL = "https://api.example.com/pillar/{minion_id}"


@pytest.fixture
def http():
    return load_module("http_pillar", {})


def response(status=200, body=None):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = body if body is not None else {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    return resp


def test_get_with_namespace(http):
    with mock.patch.object(
        http.requests, "request", return_value=response(body={"a": 1})
    ) as req:
        assert http.ext_pillar("minion1", {}, url=URL, namespace="cmdb") == {
            "cmdb": {"a": 1}
        }
    method, url = req.call_args.args
    assert (method, url) == ("GET", "https://api.example.com/pillar/minion1")
    assert req.call_args.kwargs["verify"] is True
    assert req.call_args.kwargs["timeout"] == (5, 15)


def test_minion_id_is_url_quoted(http):
    with mock.patch.object(http.requests, "request", return_value=response()) as req:
        http.ext_pillar("../admin?x=1", {}, url=URL)
    assert req.call_args.args[1] == "https://api.example.com/pillar/..%2Fadmin%3Fx%3D1"


def test_grain_placeholders_are_not_expanded(http):
    with mock.patch.object(http.requests, "request", return_value=response()) as req:
        http.ext_pillar("m", {}, url="https://api/<nodename>/%s")
    assert req.call_args.args[1] == "https://api/<nodename>/%s"


def test_auth_tls_and_post_body(http, tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    with mock.patch.object(http.requests, "request", return_value=response()) as req:
        http.ext_pillar(
            "m1",
            {},
            url="https://api/lookup",
            method="post",
            json_body={"id": "{minion_id}", "tags": ["{minion_id}", 1]},
            bearer_token_file=str(token),
            ca_bundle="/ca.pem",
            client_cert="/c.pem",
            client_key="/k.pem",
            timeout=[1, 2],
        )
    kwargs = req.call_args.kwargs
    assert req.call_args.args[0] == "POST"
    assert kwargs["headers"]["Authorization"] == "Bearer abc"
    assert kwargs["json"] == {"id": "m1", "tags": ["m1", 1]}
    assert kwargs["verify"] == "/ca.pem"
    assert kwargs["cert"] == ("/c.pem", "/k.pem")
    assert kwargs["timeout"] == (1, 2)


def test_verify_false_ignores_ca_bundle(http):
    with mock.patch.object(http.requests, "request", return_value=response()) as req:
        http.ext_pillar("m", {}, url=URL, verify=False, ca_bundle="/ca.pem")
    assert req.call_args.kwargs["verify"] is False


def test_404_is_empty_when_allowed(http):
    with mock.patch.object(http.requests, "request", return_value=response(404)):
        assert http.ext_pillar("m", {}, url=URL) == {}
        with pytest.raises(http.HttpPillarError, match="404"):
            http.ext_pillar("m", {}, url=URL, not_found_ok=False)


def test_fail_closed_and_fail_open(http):
    boom = requests.ConnectionError("refused")
    with mock.patch.object(http.requests, "request", side_effect=boom):
        with pytest.raises(http.HttpPillarError, match="refused"):
            http.ext_pillar("m", {}, url=URL)
        assert http.ext_pillar("m", {}, url=URL, fail_closed=False) == {}


def test_non_object_json_rejected(http):
    with mock.patch.object(
        http.requests, "request", return_value=response(body=[1, 2])
    ):
        with pytest.raises(http.HttpPillarError, match="JSON object"):
            http.ext_pillar("m", {}, url=URL)
