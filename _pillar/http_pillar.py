"""
Retrieve pillar data from an HTTP(S) JSON API.

Replacement for the stock ``http_json`` ext_pillar, which was removed from
Salt core in 3008. Compared to ``http_json``:

- TLS verification, custom CA bundle and client certificates
- per-endpoint connect/read timeouts
- bearer token or basic auth read from files, not inline in the master config
- any HTTP method (e.g. POST to a lookup endpoint)
- fail-closed: errors raise, Salt records them in the pillar ``_errors`` key
  and state runs refuse to execute on partial data
- the URL template only knows ``{minion_id}``; grain interpolation is
  deliberately not supported because minions control their own grains and
  could request another host's pillar

:depends: requests

Master config
=============

.. code-block:: yaml

    ext_pillar:
      - http_pillar:
          url: https://cmdb.example.com/api/v1/pillar/{minion_id}
          namespace: cmdb                                  # optional
          bearer_token_file: /etc/salt/secrets/cmdb.token  # optional
          ca_bundle: /etc/salt/pki/cmdb-ca.pem             # optional
          timeout: [5, 15]                                 # connect, read seconds

Optional ext_pillar arguments:

- ``method`` (``GET``), ``headers`` (dict), ``json_body`` (dict, sent with
  ``{minion_id}`` placeholders expanded in string values)
- ``username`` + ``password_file``: basic auth
- ``verify`` (``True``): set ``False`` to disable TLS verification (lab only)
- ``client_cert`` / ``client_key``: mutual TLS
- ``not_found_ok`` (``True``): a 404 means "no data for this minion" and returns ``{}``
- ``fail_closed`` (``True``): set ``False`` to log errors and return ``{}``
"""

import logging
import urllib.parse

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

log = logging.getLogger(__name__)

__virtualname__ = "http_pillar"


class HttpPillarError(Exception):
    """Raised when pillar data cannot be fetched and fail_closed is set."""


def __virtual__():
    if not HAS_REQUESTS:
        return (False, "http_pillar: the requests python library is not installed")
    return __virtualname__


def _read_secret(path):
    with open(path, encoding="utf-8") as fh_:
        return fh_.read().strip()


def _expand(value, minion_id):
    """Replace {minion_id} in string values of a (nested) JSON body."""
    if isinstance(value, str):
        return value.replace("{minion_id}", minion_id)
    if isinstance(value, dict):
        return {key: _expand(val, minion_id) for key, val in value.items()}
    if isinstance(value, list):
        return [_expand(val, minion_id) for val in value]
    return value


def _build_url(url, minion_id):
    if "{minion_id}" in url:
        return url.replace("{minion_id}", urllib.parse.quote(minion_id, safe=""))
    return url


def _fetch(
    minion_id,
    url,
    method,
    headers,
    json_body,
    bearer_token_file,
    username,
    password_file,
    verify,
    ca_bundle,
    client_cert,
    client_key,
    timeout,
    not_found_ok,
):
    req_headers = {"Accept": "application/json"}
    req_headers.update(headers or {})
    if bearer_token_file:
        req_headers["Authorization"] = f"Bearer {_read_secret(bearer_token_file)}"

    auth = None
    if username:
        auth = (username, _read_secret(password_file) if password_file else "")

    if verify and ca_bundle:
        verify = ca_bundle

    cert = None
    if client_cert:
        cert = (client_cert, client_key) if client_key else client_cert

    full_url = _build_url(url, minion_id)
    log.debug("http_pillar: %s %s", method, full_url)
    response = requests.request(
        method,
        full_url,
        headers=req_headers,
        json=_expand(json_body, minion_id) if json_body is not None else None,
        auth=auth,
        verify=verify,
        cert=cert,
        timeout=tuple(timeout) if isinstance(timeout, list) else timeout,
    )

    if response.status_code == 404 and not_found_ok:
        log.debug("http_pillar: no data for minion '%s' (404)", minion_id)
        return {}
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise HttpPillarError(
            f"http_pillar: expected a JSON object from {full_url}, "
            f"got {type(data).__name__}"
        )
    return data


def ext_pillar(
    minion_id,
    pillar,  # pylint: disable=unused-argument
    url,
    method="GET",
    namespace=None,
    headers=None,
    json_body=None,
    bearer_token_file=None,
    username=None,
    password_file=None,
    verify=True,
    ca_bundle=None,
    client_cert=None,
    client_key=None,
    timeout=(5, 15),
    not_found_ok=True,
    fail_closed=True,
):
    """
    Request ``url`` for ``minion_id`` and return the decoded JSON object.
    """
    try:
        data = _fetch(
            minion_id,
            url,
            method.upper(),
            headers,
            json_body,
            bearer_token_file,
            username,
            password_file,
            verify,
            ca_bundle,
            client_cert,
            client_key,
            timeout,
            not_found_ok,
        )
    except Exception as exc:  # pylint: disable=broad-except
        if fail_closed:
            raise HttpPillarError(
                f"http_pillar: request for minion '{minion_id}' failed: {exc}"
            ) from exc
        log.error("http_pillar: request for minion '%s' failed: %s", minion_id, exc)
        return {}

    if namespace:
        return {namespace: data}
    return data
