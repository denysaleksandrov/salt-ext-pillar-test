"""
Mock pillar API for local testing (stdlib only).

    python dev/mock_api.py [--port 8088] [--token secret]

GET  /pillar/<minion_id>        -> JSON object, 404 for unknown minions
POST /lookup  {"id": "<minion>"} -> same data, for method/json_body testing
GET  /pillar/<minion_id>?fail=1 -> 500
With --token, requests must send "Authorization: Bearer <token>".
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

DATA = {
    "minion1": {"app": {"version": "1.4.2", "replicas": 3}, "datacenter": "ams1"},
    "minion2": {"app": {"version": "2.0.0", "replicas": 1}, "datacenter": "fra1"},
}


class Handler(BaseHTTPRequestHandler):
    token = None

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
        if self.headers.get("Authorization") == f"Bearer {self.token}":
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _lookup(self, minion_id):
        if minion_id in DATA:
            self._send(200, DATA[minion_id])
        else:
            self._send(404, {"error": f"unknown minion {minion_id}"})

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parse_qs(parsed.query).get("fail"):
            self._send(500, {"error": "forced failure"})
        elif parsed.path.startswith("/pillar/"):
            self._lookup(unquote(parsed.path[len("/pillar/") :]))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if urlparse(self.path).path == "/lookup":
            self._lookup(body.get("id", ""))
        else:
            self._send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--token")
    args = parser.parse_args()
    Handler.token = args.token
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock pillar API on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
