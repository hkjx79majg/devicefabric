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
        if self.path == "/v1/rules":
            self.handle_service_call(lambda: (200, self.service.list_rules()))
            return
        command_ids = self._extract_device_command_ids(self.path)
        if command_ids is not None:
            device_id, command_id = command_ids
            self.handle_service_call(
                lambda: (200, self.service.get_command(device_id, command_id))
            )
            return
        device_id = self._extract_device_id(self.path, "/shadow")
        if device_id is not None:
            self.handle_service_call(lambda: (200, self.service.get_shadow(device_id)))
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

    def do_PUT(self) -> None:
        rule_id = self._extract_rule_id(self.path, "/enabled")
        if rule_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.set_rule_enabled(
                    rule_id, self.read_json_object()
                )
            self.handle_service_call(action)
            return
        device_id = self._extract_device_id(self.path, "/shadow/desired")
        if device_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.set_desired_shadow(
                    device_id, self.read_json_object()
                )
            self.handle_service_call(action)
            return
        self.send_not_found()

    def do_POST(self) -> None:
        path = self.path
        if path == "/v1/devices":
            def action() -> tuple[int, dict]:
                return 201, self.service.register_device(self.read_json_object())
            self.handle_service_call(action)
            return
        if path == "/v1/rules":
            def action() -> tuple[int, dict]:
                return 201, self.service.create_rule(self.read_json_object())
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
                return 200, self.service.subscribe_topic(session_id, self.read_json_object())
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
        session_id = self._extract_session_id(path, "/messages/ack")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.ack_messages(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/shadow/reported")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.report_shadow(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/commands/poll")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.poll_commands(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        ack_ids = self._extract_session_command_ack_ids(path)
        if ack_ids is not None:
            session_id, command_id = ack_ids
            def action() -> tuple[int, dict]:
                return 200, self.service.ack_command(
                    session_id, command_id, self.read_json_object()
                )
            self.handle_service_call(action)
            return
        device_id = self._extract_device_id(path, "/commands")
        if device_id is not None:
            def action() -> tuple[int, dict]:
                return 202, self.service.create_command(device_id, self.read_json_object())
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

    def do_DELETE(self) -> None:
        rule_id = self._extract_rule_id(self.path)
        if rule_id is not None:
            self.handle_service_call(
                lambda: (200, self.service.delete_rule(rule_id))
            )
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

    @staticmethod
    def _extract_rule_id(path: str, suffix: str = "") -> str | None:
        prefix = "/v1/rules/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        end = len(path) if not suffix else len(path) - len(suffix)
        segment = path[len(prefix):end]
        if segment and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in segment):
            return segment
        return None

    @staticmethod
    def _valid_id_segment(segment: str) -> bool:
        return bool(segment) and all(
            ch.isascii() and (ch.isalnum() or ch in "._-") for ch in segment
        )

    @classmethod
    def _extract_device_command_ids(cls, path: str) -> tuple[str, str] | None:
        """匹配 /v1/devices/{device_id}/commands/{command_id}。"""
        prefix = "/v1/devices/"
        marker = "/commands/"
        if not path.startswith(prefix) or marker not in path:
            return None
        device_segment, _, command_segment = path[len(prefix):].partition(marker)
        if cls._valid_id_segment(device_segment) and cls._valid_id_segment(command_segment):
            return device_segment, command_segment
        return None

    @classmethod
    def _extract_session_command_ack_ids(cls, path: str) -> tuple[str, str] | None:
        """匹配 /v1/device-sessions/{session_id}/commands/{command_id}/ack。"""
        prefix = "/v1/device-sessions/"
        marker = "/commands/"
        suffix = "/ack"
        if (
            not path.startswith(prefix)
            or not path.endswith(suffix)
            or marker not in path
        ):
            return None
        middle = path[len(prefix):len(path) - len(suffix)]
        session_segment, _, command_segment = middle.partition(marker)
        if cls._valid_id_segment(session_segment) and cls._valid_id_segment(command_segment):
            return session_segment, command_segment
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
