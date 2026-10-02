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

    def send_error_json(self, exc: ServiceError) -> None:
        self.send_json(
            exc.status,
            {"error": {"code": exc.code, "message": exc.message}},
        )

    def read_json_object(self) -> object:
        length = self.headers.get("Content-Length")
        if length is None or not str(length).isdigit():
            return None
        raw = self.rfile.read(int(length))
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        device_id = self._device_path(self.path)
        if device_id is not None:
            try:
                self.send_json(200, self.service.get_device(device_id))
            except ServiceError as exc:
                self.send_error_json(exc)
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path == "/v1/devices":
            self._invoke(lambda: self.service.register_device(self.read_json_object()), 201)
            return
        if self.path == "/v1/device-auth":
            self._invoke(lambda: self.service.authenticate(self.read_json_object()), 200)
            return
        device_id = self._device_path(self.path, suffix="/credential/rotate")
        if device_id is not None:
            self._invoke(lambda: self.service.rotate_credential(device_id), 200)
            return
        device_id = self._device_path(self.path, suffix="/revoke")
        if device_id is not None:
            self._invoke(lambda: self.service.revoke_device(device_id), 200)
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def _invoke(self, operation, status: int) -> None:
        try:
            self.send_json(status, operation())
        except ServiceError as exc:
            self.send_error_json(exc)

    @staticmethod
    def _device_path(path: str, suffix: str = "") -> str | None:
        prefix = "/v1/devices/"
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix):]
        if suffix:
            if not tail.endswith(suffix):
                return None
            tail = tail[: -len(suffix)]
        # Device ids never contain a slash; reject over-nested or empty paths.
        if not tail or "/" in tail:
            return None
        return tail

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
