"""HTTP entry point for DeviceFabric."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Service, ServiceError


def env_address() -> tuple[str, int]:
    raw = os.environ.get("DEVICEFABRIC_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid DEVICEFABRIC_ADDR: {raw!r}")
    return host, int(port)


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"error": {"code": code, "message": message}})

    def read_json_object(self) -> object:
        length_raw = self.headers.get("Content-Length")
        if length_raw is None or not length_raw.isdigit():
            raise ServiceError("request body must be JSON")
        body = self.rfile.read(int(length_raw))
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError("request body must be valid JSON")

    def handle_service_call(self, call) -> None:
        try:
            status, payload = call()
        except ServiceError as exc:
            self.send_error_json(exc.status, exc.code, exc.message)
            return
        self.send_json(status, payload)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        device_id = self._extract_device_id(self.path)
        if device_id is not None:
            self.handle_service_call(lambda: (200, self.service.get_device(device_id)))
            return
        session_id = self._extract_session_id(self.path)
        if session_id is not None:
            self.handle_service_call(lambda: (200, self.service.get_session(session_id)))
            return
        self.send_not_found()

    def do_POST(self) -> None:
        path = self.path
        if path == "/v1/devices":
            def action() -> tuple[int, dict]:
                return 201, self.service.register_device(self.read_json_object())
            self.handle_service_call(action)
            return
        if path == "/v1/device-auth":
            def action() -> tuple[int, dict]:
                return 200, self.service.authenticate(self.read_json_object())
            self.handle_service_call(action)
            return
        if path == "/v1/device-sessions":
            def action() -> tuple[int, dict]:
                return 201, self.service.create_session(self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/heartbeat")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.heartbeat_session(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/subscriptions")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.add_subscription(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/publish")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 202, self.service.publish_message(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/messages/poll")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.poll_messages(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        device_id = self._extract_device_id(path, "/credential/rotate")
        if device_id is not None:
            self.handle_service_call(lambda: (200, self.service.rotate_credential(device_id)))
            return
        device_id = self._extract_device_id(path, "/revoke")
        if device_id is not None:
            self.handle_service_call(lambda: (200, self.service.revoke_device(device_id)))
            return
        self.send_not_found()

    def send_not_found(self) -> None:
        self.send_error_json(404, "not_found", f"no route for {self.path}")

    @staticmethod
    def _extract_device_id(path: str, suffix: str = "") -> str | None:
        prefix = "/v1/devices/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        end = len(path) - len(suffix) if suffix else len(path)
        segment = path[len(prefix):end]
        if segment and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in segment):
            return segment
        return None

    @staticmethod
    def _extract_session_id(path: str, suffix: str = "") -> str | None:
        prefix = "/v1/device-sessions/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        end = len(path) - len(suffix) if suffix else len(path)
        segment = path[len(prefix):end]
        if segment and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in segment):
            return segment
        return None

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging so recorded output stays stable."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="devicefabric.server", description="物联网设备接入、编排与治理平台")
    host, port = env_address()
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"DeviceFabric listening on http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
