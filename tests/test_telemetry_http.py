import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError
from urllib.parse import urlencode

from devicefabric.server import Handler
from devicefabric.service import Service


class TelemetryHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的 Service，保证进程内状态互不影响。
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, self.device = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-1", "display_name": "传感器"}
        )
        self.assertEqual(status, 201)
        status, self.session = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        self.assertEqual(status, 201)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def submit(self, request_id="req-1", points=None):
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1.5}
            ]
        return self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/telemetry",
            {
                "session_token": self.session["session_token"],
                "request_id": request_id,
                "points": points,
            },
        )

    def query(self, **params):
        defaults = {
            "metric": "temp",
            "start": "2025-12-31T12:00:00Z",
            "end": "2026-01-01T12:00:00Z",
            "resolution": "raw",
        }
        defaults.update(params)
        return self.call(
            "GET", f"/v1/devices/sensor-1/telemetry?{urlencode(defaults)}"
        )

    def test_submit_and_query_flow(self) -> None:
        status, result = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:10+00:00", "value": 1},
            {"metric": "temp", "timestamp": "2026-01-01T00:00:20Z", "value": 2},
        ])
        self.assertEqual(status, 202)
        self.assertEqual(result, {"request_id": "req-1", "accepted_count": 2})

        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(result["device_id"], "sensor-1")
        self.assertEqual(result["metric"], "temp")
        self.assertEqual(result["resolution"], "raw")
        self.assertEqual(
            result["points"],
            [
                {"timestamp": "2026-01-01T00:00:10Z", "value": 1},
                {"timestamp": "2026-01-01T00:00:20Z", "value": 2},
            ],
        )

        status, result = self.query(resolution="60")
        self.assertEqual(status, 200)
        self.assertEqual(
            result["buckets"],
            [{"start": "2026-01-01T00:00:00Z", "count": 2,
              "min": 1, "max": 2, "avg": 1.5, "last": 2}],
        )

    def test_submit_idempotent_replay(self) -> None:
        status, first = self.submit()
        self.assertEqual(status, 202)
        status, second = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(first, second)
        status, result = self.query()
        self.assertEqual(len(result["points"]), 1)

    def test_submit_conflict(self) -> None:
        self.submit()
        status, error = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 9}
        ])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "telemetry_request_conflict")

    def test_submit_session_errors(self) -> None:
        payload = {
            "session_token": self.session["session_token"],
            "request_id": "req-1",
            "points": [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1}
            ],
        }
        status, error = self.call(
            "POST", "/v1/device-sessions/unknown/telemetry", payload
        )
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "session_not_found")

        status, error = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/telemetry",
            dict(payload, session_token="wrong"),
        )
        self.assertEqual(status, 401)
        self.assertEqual(error["error"]["code"], "invalid_session_token")

    def test_submit_invalid_request(self) -> None:
        status, error = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/telemetry",
            {"session_token": self.session["session_token"], "request_id": "r"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_request")

    def test_query_errors(self) -> None:
        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(result["points"], [])

        status, error = self.call(
            "GET", "/v1/devices/no-such/telemetry?"
            "metric=temp&start=2026-01-01T00:00:00Z"
            "&end=2026-01-02T00:00:00Z&resolution=raw"
        )
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "device_not_found")

        status, error = self.call(
            "GET", "/v1/devices/sensor-1/telemetry?metric=temp"
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_request")

        status, error = self.call(
            "GET", "/v1/devices/sensor-1/telemetry"
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_request")

    def test_query_with_offset_timestamps(self) -> None:
        # 查询参数中的 + 必须 URL 编码；urlencode 会处理。
        self.submit()
        status, result = self.query(
            start="2025-12-31T16:00:00-08:00", end="2026-01-02T08:00:00+08:00"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["points"]), 1)


if __name__ == "__main__":
    unittest.main()
