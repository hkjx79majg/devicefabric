import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class RateLimitHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的 Service，保证进程内状态互不影响。
        service = Service(publish_rate_limit=2, telemetry_point_rate_limit=2)
        handler_cls = type("TestHandler", (Handler,), {"service": service})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, self.device = self.call(
            "POST", "/v1/devices", {"device_id": "dev-1", "display_name": "设备"}
        )
        self.assertEqual(status, 201)
        status, self.session = self.call("POST", "/v1/device-sessions", {
            "device_id": "dev-1",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 300,
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

    def call_with_headers(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), json.loads(
                    resp.read().decode("utf-8")
                )
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            response_headers = dict(exc.headers)
            exc.close()
            return status, response_headers, payload

    def publish(self, payload=1):
        return self.call_with_headers(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/publish",
            {
                "session_token": self.session["session_token"],
                "topic": "a/b",
                "payload": payload,
            },
        )

    def submit(self, request_id, count=1):
        return self.call_with_headers(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/telemetry",
            {
                "session_token": self.session["session_token"],
                "request_id": request_id,
                "points": [
                    {
                        "metric": "temp",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "value": index,
                    }
                    for index in range(count)
                ],
            },
        )

    def assert_unified_429(self, status, headers, payload):
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "rate_limit_exceeded")
        self.assertIn("message", payload["error"])
        retry_after = int(headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, 60)

    def test_publish_429_carries_unified_error_and_retry_after(self):
        status, _, _ = self.publish()
        self.assertEqual(status, 202)
        status, _, _ = self.publish()
        self.assertEqual(status, 202)
        status, headers, payload = self.publish()
        self.assert_unified_429(status, headers, payload)

    def test_telemetry_429_carries_unified_error_and_retry_after(self):
        status, _, _ = self.submit("req-1", count=2)
        self.assertEqual(status, 202)
        status, headers, payload = self.submit("req-2", count=1)
        self.assert_unified_429(status, headers, payload)
        # 幂等重试仍返回原有 202 结果。
        status, _, payload = self.submit("req-1", count=2)
        self.assertEqual(status, 202)
        self.assertEqual(
            payload, {"request_id": "req-1", "accepted_count": 2}
        )

    def test_other_endpoints_unaffected_by_limits(self):
        # 用尽两种额度后，健康检查与其他入口保持既有语义。
        self.publish()
        self.publish()
        self.submit("req-1", count=2)
        status, payload = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        status, payload = self.call(
            "GET", f"/v1/device-sessions/{self.session['session_id']}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["online"])
        status, payload = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/messages/poll",
            {
                "session_token": self.session["session_token"],
                "max_messages": 10,
            },
        )
        self.assertEqual(status, 200)
        status, payload = self.call(
            "GET",
            "/v1/devices/dev-1/telemetry?metric=temp"
            "&start=2025-12-31T12:00:00Z&end=2026-01-01T12:00:00Z"
            "&resolution=raw",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["points"]), 2)


if __name__ == "__main__":
    unittest.main()
