"""
Retrieve pillar data from PostgreSQL (jsonb rows merged in order).

Replacement for the stock ``postgres`` ext_pillar with:

- named connection profiles (several databases per master), any libpq option
  (``sslmode``, ``sslrootcert``, ``connect_timeout`` ...), password from a file
  or environment variable instead of inline in the master config
- read-only session with ``statement_timeout``
- one connection per master worker process, reused across pillar renders
- fail-closed: a DB error raises, Salt records it in the pillar ``_errors``
  key and state runs refuse to execute on partial data
- ``%(minion_id)s`` is the only bind parameter; grains are deliberately not
  available because minions control their own grains

:depends: psycopg2

Master config
=============

.. code-block:: yaml

    pg_pillar:
      profiles:
        main:
          host: pg.example.com
          port: 5432
          dbname: salt
          user: salt_pillar
          password_file: /etc/salt/secrets/pg_pillar.pw   # or password_env: PG_PILLAR_PW
          sslmode: verify-full
          sslrootcert: /etc/salt/pki/pg-ca.pem
          connect_timeout: 5
          statement_timeout_ms: 5000

    ext_pillar:
      - pg_pillar:
          profile: main
          queries:
            - SELECT data FROM pillar_data WHERE target = 'default' ORDER BY priority
            - SELECT data FROM pillar_data WHERE target = %(minion_id)s ORDER BY priority

Each query must return a single json/jsonb column whose values are objects.
Rows are merged in order (later rows win) using the master's
``pillar_source_merging_strategy`` and ``pillar_merge_lists``.

Optional ext_pillar arguments:

- ``namespace``: nest the result under this pillar key
- ``fail_closed`` (default ``True``): set ``False`` to log errors and return
  ``{}`` like the stock module
"""

import json
import logging
import os
import sys
import types

import salt.utils.dictupdate

try:
    import psycopg2
    import psycopg2.extensions

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

log = logging.getLogger(__name__)

__virtualname__ = "pg_pillar"

# Options consumed by this module rather than passed to psycopg2.connect().
_OWN_OPTIONS = ("password_file", "password_env", "statement_timeout_ms")

# The Salt loader re-executes this module for every pillar compile, so a plain
# module-level dict would not survive between renders. Keep the connection
# cache in a process-wide holder instead.
_REGISTRY_NAME = "_salt_pg_pillar_connections"


class PgPillarError(Exception):
    """Raised when pillar data cannot be fetched and fail_closed is set."""


def __virtual__():
    if not HAS_PSYCOPG2:
        return (False, "pg_pillar: the psycopg2 python library is not installed")
    return __virtualname__


def _registry():
    holder = sys.modules.get(_REGISTRY_NAME)
    if holder is None:
        holder = types.ModuleType(_REGISTRY_NAME)
        holder.connections = {}
        holder.pid = os.getpid()
        sys.modules[_REGISTRY_NAME] = holder
    if holder.pid != os.getpid():
        # Forked child: never reuse the parent's sockets.
        holder.connections = {}
        holder.pid = os.getpid()
    return holder.connections


def _profile_options(profile):
    profiles = __opts__.get("pg_pillar", {}).get("profiles", {})
    if profile not in profiles:
        raise PgPillarError(f"pg_pillar profile '{profile}' is not configured")
    options = dict(profiles[profile])

    password_file = options.pop("password_file", None)
    password_env = options.pop("password_env", None)
    if password_file:
        with open(password_file, encoding="utf-8") as fh_:
            options["password"] = fh_.read().strip()
    elif password_env:
        if password_env not in os.environ:
            raise PgPillarError(
                f"pg_pillar profile '{profile}': environment variable "
                f"'{password_env}' is not set"
            )
        options["password"] = os.environ[password_env]

    timeout_ms = options.pop("statement_timeout_ms", None)
    if timeout_ms is not None:
        extra = options.get("options", "")
        options["options"] = f"{extra} -c statement_timeout={int(timeout_ms)}".strip()

    for key in _OWN_OPTIONS:
        options.pop(key, None)
    return options


def _connect(profile):
    options = _profile_options(profile)
    options.setdefault("application_name", "salt-pg-pillar")
    conn = psycopg2.connect(**options)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _get_connection(profile):
    connections = _registry()
    conn = connections.get(profile)
    if conn is None or conn.closed:
        conn = _connect(profile)
        connections[profile] = conn
    return conn


def _drop_connection(profile):
    conn = _registry().pop(profile, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pylint: disable=broad-except
            pass


def _row_to_dict(value, query):
    if value is None:
        return {}
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise PgPillarError(
            f"pg_pillar: query must return JSON objects, got "
            f"{type(value).__name__}: {query}"
        )
    return value


def _merge(base, update):
    return salt.utils.dictupdate.merge(
        base,
        update,
        strategy=__opts__.get("pillar_source_merging_strategy", "smart"),
        renderer=__opts__.get("renderer", "yaml"),
        merge_lists=__opts__.get("pillar_merge_lists", False),
    )


def _run_queries(conn, minion_id, queries):
    ret = {}
    params = {"minion_id": minion_id}
    with conn.cursor() as cursor:
        for query in queries:
            cursor.execute(query, params)
            for row in cursor.fetchall():
                ret = _merge(ret, _row_to_dict(row[0], query))
    return ret


def ext_pillar(
    minion_id,
    pillar,  # pylint: disable=unused-argument
    queries=None,
    profile="main",
    namespace=None,
    fail_closed=True,
):
    """
    Run ``queries`` against the ``profile`` database and merge the resulting
    JSON objects into a pillar dict.
    """
    if not queries:
        raise PgPillarError("pg_pillar: no queries configured")
    if isinstance(queries, str):
        queries = [queries]

    try:
        try:
            data = _run_queries(_get_connection(profile), minion_id, queries)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            # A cached connection may have been closed by the server or a
            # network hiccup; reconnect once before giving up.
            log.warning(
                "pg_pillar: connection for profile '%s' failed (%s), reconnecting",
                profile,
                exc,
            )
            _drop_connection(profile)
            data = _run_queries(_get_connection(profile), minion_id, queries)
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, psycopg2.Error):
            _drop_connection(profile)
        if fail_closed:
            raise PgPillarError(
                f"pg_pillar: profile '{profile}' failed for minion '{minion_id}': {exc}"
            ) from exc
        log.error(
            "pg_pillar: profile '%s' failed for minion '%s': %s",
            profile,
            minion_id,
            exc,
        )
        return {}

    if namespace:
        return {namespace: data}
    return data
