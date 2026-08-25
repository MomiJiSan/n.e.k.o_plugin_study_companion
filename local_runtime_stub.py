"""Minimal, local-only runtime used before model installation exists.

It is intentionally implemented with the standard library: the runtime has no
outbound network capability, does not import model libraries, and never logs a
request body.  The parent process owns the random session token.
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, TextIO

try:
    from .local_runtime_protocol import (
        LOCAL_MODELS_NOT_INSTALLED,
        LOCAL_RUNTIME_AUTH_FAILED,
        PROTOCOL_VERSION,
        TOKEN_HEADER,
        error_payload,
        ready_status,
    )
except ImportError:  # Direct script execution by the supervisor.
    from local_runtime_protocol import (  # type: ignore[no-redef]
        LOCAL_MODELS_NOT_INSTALLED,
        LOCAL_RUNTIME_AUTH_FAILED,
        PROTOCOL_VERSION,
        TOKEN_HEADER,
        error_payload,
        ready_status,
    )


LOOPBACK_HOST: Final = "127.0.0.1"


class _RuntimeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], token: str) -> None:
        self.session_token = token
        super().__init__(address, _RuntimeRequestHandler)


class _RuntimeRequestHandler(BaseHTTPRequestHandler):
    server: _RuntimeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        """Never log request paths, bodies, tokens, or user content."""

    def _is_authorized(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "")
        return bool(supplied) and hmac.compare_digest(supplied, self.server.session_token)

    def _read_and_discard_body(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length > 0:
            self.rfile.read(content_length)

    def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self) -> bool:
        if self._is_authorized():
            return True
        self._write_json(
            HTTPStatus.UNAUTHORIZED, error_payload(LOCAL_RUNTIME_AUTH_FAILED)
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, ready_status().to_payload())
            return
        if self.path == "/runtime/status":
            self._write_json(HTTPStatus.OK, ready_status().to_payload())
            return
        self._write_json(HTTPStatus.NOT_FOUND, error_payload("not_found"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        self._read_and_discard_body()
        if self.path == "/runtime/unload":
            self._write_json(HTTPStatus.OK, ready_status().to_payload())
            return
        if self.path in {"/v1/math/recognize", "/v1/math/explain"}:
            self._write_json(
                HTTPStatus.NOT_IMPLEMENTED, error_payload(LOCAL_MODELS_NOT_INSTALLED)
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, error_payload("not_found"))


def serve(*, token: str, ready_stream: TextIO = sys.stdout) -> None:
    """Start the local-only runtime and announce its selected ephemeral port."""

    if not token:
        raise ValueError("a runtime session token is required")
    with _RuntimeServer((LOOPBACK_HOST, 0), token) as server:
        event = {
            "event": "ready",
            "port": int(server.server_port),
            "protocol_version": PROTOCOL_VERSION,
        }
        ready_stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        ready_stream.flush()
        server.serve_forever(poll_interval=0.25)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local math runtime stub")
    parser.add_argument("--token", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    serve(token=args.token)


if __name__ == "__main__":
    main()
