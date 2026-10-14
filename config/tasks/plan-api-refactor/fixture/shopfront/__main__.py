"""python3 -m shopfront  -- boot the HTTP server."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shopfront import api


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        resp = api.handle(method, self.path, dict(self.headers), body)
        payload = json.dumps(resp["body"]).encode()
        self.send_response(resp["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, *args):  # keep test output quiet
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
    print("shopfront listening on http://127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
