"""HTTP entry point for DeviceFabric."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import RateLimitError, Service, ServiceError

# 路径匹配到了对应路由但标识符段非法：返回 400（invalid_request），
# 而不是按未匹配处理为 404。
INVALID_ID_SEGMENT = object()


def env_address() -> tuple[str, int]:
    raw = os.environ.get("DEVICEFABRIC_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid DEVICEFABRIC_ADDR: {raw!r}")
    return host, int(port)


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(
        self, status: int, payload: dict, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_json(
            status, {"error": {"code": code, "message": message}}, headers=headers
        )

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
        except RateLimitError as exc:
            # 统一错误体之外额外携带 Retry-After（到下一个 UTC 分钟边界的
            # 向上取整秒数）。RateLimitError 是 ServiceError 的子类，必须
            # 先于 ServiceError 捕获。
            self.send_error_json(
                exc.status,
                exc.code,
                exc.message,
                headers={"Retry-After": str(exc.retry_after)},
            )
            return
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
        audit_query = self._extract_audit_query(self.path)
        if audit_query is not None:
            self.handle_service_call(
                lambda: (200, self.service.query_audit_events(audit_query))
            )
            return
        group_batch = self._extract_group_batch_ids(self.path)
        if group_batch is not None:
            if group_batch is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            group_id, batch_id = group_batch
            self.handle_service_call(
                lambda: (200, self.service.get_command_batch(group_id, batch_id))
            )
            return
        group_rollout = self._extract_group_rollout_ids(self.path)
        if group_rollout is not None:
            if group_rollout is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            group_id, rollout_id = group_rollout
            self.handle_service_call(
                lambda: (200, self.service.get_firmware_rollout(group_id, rollout_id))
            )
            return
        group_id = self._extract_group_id(self.path)
        if group_id is not None:
            if group_id is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            self.handle_service_call(lambda: (200, self.service.get_group(group_id)))
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
        telemetry = self._extract_device_telemetry_query(self.path)
        if telemetry is not None:
            device_id, query = telemetry
            self.handle_service_call(
                lambda: (200, self.service.query_telemetry(device_id, query))
            )
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
        group_members = self._extract_group_id(self.path, "/members")
        if group_members is not None:
            if group_members is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            def action() -> tuple[int, dict]:
                return 200, self.service.replace_group_members(
                    group_members, self.read_json_object()
                )
            self.handle_service_call(action)
            return
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
        if path == "/v1/device-groups":
            def action() -> tuple[int, dict]:
                return 201, self.service.create_group(self.read_json_object())
            self.handle_service_call(action)
            return
        if path == "/v1/firmware-releases":
            def action() -> tuple[int, dict]:
                return 201, self.service.create_firmware_release(
                    self.read_json_object()
                )
            self.handle_service_call(action)
            return
        group_rollouts = self._extract_group_id(path, "/firmware-rollouts")
        if group_rollouts is not None:
            if group_rollouts is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            group_id: str = group_rollouts
            def action() -> tuple[int, dict]:
                return 202, self.service.create_firmware_rollout(
                    group_id, self.read_json_object()
                )
            self.handle_service_call(action)
            return
        group_batches = self._extract_group_id(path, "/command-batches")
        if group_batches is not None:
            if group_batches is INVALID_ID_SEGMENT:
                self.send_invalid_identifier()
                return
            group_id: str = group_batches
            def action() -> tuple[int, dict]:
                return 202, self.service.create_command_batch(
                    group_id, self.read_json_object()
                )
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
        session_id = self._extract_session_id(path, "/telemetry")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 202, self.service.submit_telemetry(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/commands/poll")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.poll_commands(session_id, self.read_json_object())
            self.handle_service_call(action)
            return
        session_id = self._extract_session_id(path, "/firmware/poll")
        if session_id is not None:
            def action() -> tuple[int, dict]:
                return 200, self.service.poll_firmware_update(
                    session_id, self.read_json_object()
                )
            self.handle_service_call(action)
            return
        firmware_ack_ids = self._extract_session_firmware_ack_ids(path)
        if firmware_ack_ids is not None:
            session_id, update_id = firmware_ack_ids
            def action() -> tuple[int, dict]:
                return 200, self.service.ack_firmware_update(
                    session_id, update_id, self.read_json_object()
                )
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

    def send_invalid_identifier(self) -> None:
        self.send_error_json(
            400,
            "invalid_request",
            "identifier must be 1-64 ASCII letters, digits, dots, "
            "underscores or hyphens",
        )

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
    def _extract_device_telemetry_query(path: str) -> tuple[str, str] | None:
        """匹配 /v1/devices/{device_id}/telemetry[?query]，返回设备与查询串。"""
        prefix = "/v1/devices/"
        suffix = "/telemetry"
        bare, _, query = path.partition("?")
        if not bare.startswith(prefix) or not bare.endswith(suffix):
            return None
        segment = bare[len(prefix):len(bare) - len(suffix)]
        if segment and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in segment):
            return segment, query
        return None

    @staticmethod
    def _extract_audit_query(path: str) -> str | None:
        """匹配 /v1/audit-events[?query]，返回查询串（可为空串）。"""
        bare, _, query = path.partition("?")
        if bare != "/v1/audit-events":
            return None
        return query

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
    def _extract_group_id(path: str, suffix: str = "") -> str | object | None:
        """匹配 /v1/device-groups/{group_id}[/suffix]。

        路径形状不匹配返回 None；匹配但 group_id 段非法返回
        INVALID_ID_SEGMENT（调用方据此回 400）；否则返回合法 group_id。
        """
        prefix = "/v1/device-groups/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        end = len(path) - len(suffix) if suffix else len(path)
        segment = path[len(prefix):end]
        # 多段路径不匹配本路由形状（交由后续路由，最终 404）；单段非法
        # 标识则为 400。
        if not segment or "/" in segment:
            return None
        if Handler._valid_id_segment(segment):
            return segment
        return INVALID_ID_SEGMENT

    @classmethod
    def _extract_group_batch_ids(cls, path: str) -> tuple[str, str] | object | None:
        """匹配 /v1/device-groups/{group_id}/command-batches/{batch_id}。

        形状不匹配返回 None；两个标识均为单段但任一非法返回
        INVALID_ID_SEGMENT。
        """
        prefix = "/v1/device-groups/"
        marker = "/command-batches/"
        if not path.startswith(prefix) or marker not in path:
            return None
        group_segment, _, batch_segment = path[len(prefix):].partition(marker)
        if not group_segment or not batch_segment:
            return None
        if "/" in group_segment or "/" in batch_segment:
            return None
        if not (
            cls._valid_id_segment(group_segment)
            and cls._valid_id_segment(batch_segment)
        ):
            return INVALID_ID_SEGMENT
        return group_segment, batch_segment

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

    @classmethod
    def _extract_group_rollout_ids(cls, path: str) -> tuple[str, str] | object | None:
        """匹配 /v1/device-groups/{group_id}/firmware-rollouts/{rollout_id}。

        形状不匹配返回 None；两个标识均为单段但任一非法返回
        INVALID_ID_SEGMENT。
        """
        prefix = "/v1/device-groups/"
        marker = "/firmware-rollouts/"
        if not path.startswith(prefix) or marker not in path:
            return None
        group_segment, _, rollout_segment = path[len(prefix):].partition(marker)
        if not group_segment or not rollout_segment:
            return None
        if "/" in group_segment or "/" in rollout_segment:
            return None
        if not (
            cls._valid_id_segment(group_segment)
            and cls._valid_id_segment(rollout_segment)
        ):
            return INVALID_ID_SEGMENT
        return group_segment, rollout_segment

    @classmethod
    def _extract_session_firmware_ack_ids(cls, path: str) -> tuple[str, str] | None:
        """匹配 /v1/device-sessions/{session_id}/firmware/{update_id}/ack。"""
        prefix = "/v1/device-sessions/"
        marker = "/firmware/"
        suffix = "/ack"
        if (
            not path.startswith(prefix)
            or not path.endswith(suffix)
            or marker not in path
        ):
            return None
        middle = path[len(prefix):len(path) - len(suffix)]
        session_segment, _, update_segment = middle.partition(marker)
        if cls._valid_id_segment(session_segment) and cls._valid_id_segment(update_segment):
            return session_segment, update_segment
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
