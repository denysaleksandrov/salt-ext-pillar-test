"""
Simple pillar API server for the ``http_pillar`` ext_pillar.

Serves per-minion pillar data from a dict file (YAML/JSON) or from the
PostgreSQL ``pillar_data`` table (see ``sql/schema.sql``). Stdlib only;
psycopg2 is needed for the postgres backend and PyYAML for YAML files.

Endpoints
=========

``GET  /pillar/<minion_id>``        pillar for the minion
``POST /lookup {"id": "<minion>"}`` same, for ``method: POST`` + ``json_body``
``GET  /healthz``                   liveness (checks the DB for postgres)

Data for a minion is the ``default`` entry deep-merged with the minion's own
entry (dicts merge recursively, everything else is replaced). The response is
404 when neither exists, which ``http_pillar`` treats as "no data".

Usage
=====

.. code-block:: bash

    # dict backend (file is re-read when it changes)
    python api/pillar_api.py dict api/example_pillars.yaml

    # postgres backend (any libpq conninfo; password via PGPASSWORD or ~/.pgpass)
    python api/pillar_api.py postgres "host=pg.example.com dbname=salt user=salt_pillar sslmode=verify-full"

    # common options
    --host 0.0.0.0 --port 8088
    --token-file /etc/pillar-api/token      # or PILLAR_API_TOKEN env; require "Bearer <token>"
    --tls-cert server.pem --tls-key server.key
"""

import argparse
import hmac
import json
import logging
import os
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

log = logging.getLogger("pillar_api")

DEFAULT_TARGET = "default"
DEFAULT_QUERY = (
    "SELECT data FROM pillar_data WHERE target IN (%(default)s, %(minion_id)s) "
    "ORDER BY (target <> %(default)s), priority"
)


def deep_merge(base, update):
    """Return ``base`` with ``update`` merged in; dicts recurse, others replace."""
    ret = dict(base)
    for key, val in update.items():
        if isinstance(val, dict) and isinstance(ret.get(key), dict):
            ret[key] = deep_merge(ret[key], val)
        else:
            ret[key] = val
    return ret


class DictBackend:
    """Pillars from a YAML/JSON file: ``{default: {...}, <minion_id>: {...}}``."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self._data = {}
        self._load()

    def _load(self):
        mtime = os.stat(self.path).st_mtime_ns
        if mtime == self._mtime:
            return
        with open(self.path, encoding="utf-8") as fh_:
            if self.path.endswith((".yaml", ".yml")):
                import yaml  # pylint: disable=import-outside-toplevel

                data = yaml.safe_load(fh_) or {}
            else:
                data = json.load(fh_)
        if not isinstance(data, dict) or not all(
            isinstance(val, dict) for val in data.values()
        ):
            raise ValueError(f"{self.path}: expected a mapping of target -> mapping")
        self._data, self._mtime = data, mtime
        log.info("loaded %d targets from %s", len(data), self.path)

    def get(self, minion_id):
        with self._lock:
            try:
                self._load()
            except Exception:  # pylint: disable=broad-except
                # Keep serving the last good copy while the file is being edited.
                log.exception("reloading %s failed, serving previous data", self.path)
            data = self._data
        if DEFAULT_TARGET not in data and minion_id not in data:
            return None
        return deep_merge(data.get(DEFAULT_TARGET, {}), data.get(minion_id, {}))

    def health(self):
        return True


class PostgresBackend:
    """Pillars from ``pillar_data`` rows (one jsonb object per row)."""

    def __init__(self, dsn, query=DEFAULT_QUERY, pool_size=5):
        import psycopg2.pool  # pylint: disable=import-outside-toplevel

        self._errors = (psycopg2.OperationalError, psycopg2.InterfaceError)
        self.query = query
        self.pool = psycopg2.pool.ThreadedConnectionPool(1, pool_size, dsn)

    def _execute(self, query, params):
        conn = self.pool.getconn()
        broken = False
        try:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall() if cursor.description else []
        except self._errors:
            broken = True
            raise
        finally:
            self.pool.putconn(conn, close=broken)

    def get(self, minion_id):
        rows = self._execute(
            self.query, {"default": DEFAULT_TARGET, "minion_id": minion_id}
        )
        if not rows:
            return None
        ret = {}
        for (value,) in rows:
            if isinstance(value, (str, bytes)):
                value = json.loads(value)
            if isinstance(value, dict):
                ret = deep_merge(ret, value)
        return ret

    def health(self):
        self._execute("SELECT 1", None)
        return True


class PillarHandler(BaseHTTPRequestHandler):
    backend = None
    token = None
    server_version = "pillar-api/1"

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        log.info("%s %s", self.address_string(), format % args)

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self):
        if not self.token:
            return True
        supplied = self.headers.get("Authorization", "")
        if hmac.compare_digest(supplied.encode(), f"Bearer {self.token}".encode()):
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _serve_pillar(self, minion_id):
        if not minion_id:
            self._send(400, {"error": "missing minion id"})
            return
        try:
            data = self.backend.get(minion_id)
        except Exception:  # pylint: disable=broad-except
            log.exception("backend lookup failed for %s", minion_id)
            self._send(503, {"error": "backend unavailable"})
            return
        if data is None:
            self._send(404, {"error": f"no pillar data for {minion_id}"})
        else:
            self._send(200, data)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            try:
                self.backend.health()
                self._send(200, {"status": "ok"})
            except Exception:  # pylint: disable=broad-except
                log.exception("health check failed")
                self._send(503, {"status": "backend unavailable"})
            return
        if not self._authorized():
            return
        if path.startswith("/pillar/"):
            self._serve_pillar(unquote(path[len("/pillar/") :]))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        if urlparse(self.path).path != "/lookup":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            minion_id = body.get("id") if isinstance(body, dict) else None
        except ValueError:
            self._send(400, {"error": "invalid JSON body"})
            return
        self._serve_pillar(minion_id if isinstance(minion_id, str) else None)


def make_server(backend, host="127.0.0.1", port=8088, token=None, tls_cert=None, tls_key=None):
    handler = type("Handler", (PillarHandler,), {"backend": backend, "token": token})
    server = ThreadingHTTPServer((host, port), handler)
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Serve Salt pillar data over HTTP.")
    sub = parser.add_subparsers(dest="backend", required=True)
    dict_p = sub.add_parser("dict", help="serve from a YAML/JSON file")
    dict_p.add_argument("path")
    pg_p = sub.add_parser("postgres", help="serve from the pillar_data table")
    pg_p.add_argument("dsn", help="libpq conninfo string or postgresql:// URI")
    pg_p.add_argument("--query", default=DEFAULT_QUERY,
                      help="SQL with %%(minion_id)s and %%(default)s placeholders")
    pg_p.add_argument("--pool-size", type=int, default=5)
    for sub_p in (dict_p, pg_p):
        sub_p.add_argument("--host", default="127.0.0.1")
        sub_p.add_argument("--port", type=int, default=8088)
        sub_p.add_argument("--token-file", help="require 'Authorization: Bearer <token>'")
        sub_p.add_argument("--tls-cert")
        sub_p.add_argument("--tls-key")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)

    if args.backend == "dict":
        backend = DictBackend(args.path)
    else:
        backend = PostgresBackend(args.dsn, args.query, args.pool_size)

    token = os.environ.get("PILLAR_API_TOKEN")
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as fh_:
            token = fh_.read().strip()
    if not token:
        log.warning("no token configured: the API is unauthenticated")

    server = make_server(backend, args.host, args.port, token, args.tls_cert, args.tls_key)
    scheme = "https" if args.tls_cert else "http"
    log.info("serving %s backend on %s://%s:%d", args.backend, scheme, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
