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

    def submit(self, request_id="req-1", points=None, token=None):
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 21.5}
            ]
        return self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/telemetry",
            {
                "session_token": token if token is not None
                else self.session["session_token"],
                "request_id": request_id,
                "points": points,
            },
        )

    def query(self, metric="temp", start="2026-10-01T00:00:00Z",
              end="2026-10-02T00:00:00Z", resolution="raw", device_id="sensor-1",
              extra=""):
        params = urlencode({
            "metric": metric, "start": start, "end": end, "resolution": resolution,
        })
        return self.call(
            "GET", f"/v1/devices/{device_id}/telemetry?{params}{extra}"
        )

    def test_submit_and_raw_query_roundtrip(self):
        status, body = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-10-01T00:00:02Z", "value": 3},
            {"metric": "temp", "timestamp": "2026-10-01T00:00:01Z", "value": 2},
        ])
        self.assertEqual(status, 202)
        self.assertEqual(body, {"request_id": "req-1", "accepted_count": 2})

        status, body = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "sensor-1")
        self.assertEqual(body["metric"], "temp")
        self.assertEqual(body["resolution"], "raw")
        self.assertEqual(
            body["points"],
            [
                {"metric": "temp", "timestamp": "2026-10-01T00:00:01Z", "value": 2},
                {"metric": "temp", "timestamp": "2026-10-01T00:00:02Z", "value": 3},
            ],
        )

    def test_submit_idempotent_replay_and_conflict(self):
        points = [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        ]
        status, first = self.submit("req-1", points)
        self.assertEqual(status, 202)
        status, replay = self.submit("req-1", points)
        self.assertEqual(status, 202)
        self.assertEqual(first, replay)

        status, body = self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 2}
        ])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "telemetry_request_conflict")

    def test_submit_validation_and_session_errors(self):
        status, body = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": True}
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call(
            "POST", "/v1/device-sessions/no-such/telemetry",
            {"session_token": "x", "request_id": "r",
             "points": [{"metric": "temp",
                         "timestamp": "2026-10-01T00:00:00Z", "value": 1}]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "session_not_found")

        status, body = self.submit(token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_session_token")

    def test_downsampled_query_over_http(self):
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-10-01T00:00:30+00:00", "value": 4},
            {"metric": "temp", "timestamp": "2026-10-01T00:05:30Z", "value": 8},
        ])
        status, body = self.query(resolution="300")
        self.assertEqual(status, 200)
        self.assertEqual(body["resolution"], "300")
        self.assertEqual(len(body["windows"]), 2)
        first = body["windows"][0]
        self.assertEqual(first["start"], "2026-10-01T00:00:00Z")
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["min"], 4)
        self.assertEqual(first["max"], 4)
        self.assertEqual(first["avg"], 4)
        self.assertEqual(first["last"], 4)
        second = body["windows"][1]
        self.assertEqual(second["start"], "2026-10-01T00:05:00Z")
        self.assertEqual(second["last"], 8)

    def test_query_errors(self):
        # 缺少参数。
        status, body = self.call(
            "GET", f"/v1/devices/sensor-1/telemetry?{urlencode({'metric': 'temp'})}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        # 未知参数。
        status, body = self.query(extra="&foo=bar")
        self.assertEqual(status, 400)

        # 重复参数。
        status, body = self.query(extra="&metric=temp")
        self.assertEqual(status, 400)

        # 非法 resolution。
        status, body = self.query(resolution="30")
        self.assertEqual(status, 400)

        # raw 超过 24 小时。
        status, body = self.query(end="2026-10-03T00:00:01Z")
        self.assertEqual(status, 400)

        # 设备不存在。
        status, body = self.query(device_id="no-such-device")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    def test_query_empty_result(self):
        status, body = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(body["points"], [])


if __name__ == "__main__":
    unittest.main()
