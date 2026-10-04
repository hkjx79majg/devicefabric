"""按设备固定窗口限流的 HTTP 层测试：429 统一错误体与 Retry-After 头。"""

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
        service = Service(publish_rate_limit=2, telemetry_point_rate_limit=5)
        handler_cls = type("TestHandler", (Handler,), {"service": service})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, self.device, _ = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-1", "display_name": "传感器"}
        )
        self.assertEqual(status, 201)
        status, self.session, _ = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 3600,
        })
        self.assertEqual(status, 201)
        status, other, _ = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-2", "display_name": "二号"}
        )
        self.assertEqual(status, 201)
        status, self.other_session, _ = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-2",
            "credential": other["credential"],
            "client_id": "cli-2",
            "keepalive_seconds": 3600,
        })
        self.assertEqual(status, 201)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None):
        """返回 (status, payload, headers)，错误响应同样解析统一错误体。"""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(req) as resp:
                return (
                    resp.status,
                    json.loads(resp.read().decode("utf-8")),
                    resp.headers,
                )
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            response_headers = exc.headers
            exc.close()
            return exc.code, payload, response_headers

    def publish(self, session=None, topic="t/data", payload=1):
        session = session or self.session
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish",
            {"session_token": session["session_token"],
             "topic": topic, "payload": payload},
        )

    def submit(self, request_id, points=None, session=None):
        session = session or self.session
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                 "value": 1}
            ]
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}/telemetry",
            {"session_token": session["session_token"],
             "request_id": request_id, "points": points},
        )

    def point(self, minute=0):
        return {"metric": "temp",
                "timestamp": f"2026-01-01T00:{minute:02d}:00Z", "value": 1}

    def assert_retry_after(self, headers) -> int:
        value = headers.get("Retry-After")
        self.assertIsNotNone(value, "429 response must carry Retry-After")
        self.assertTrue(value.isdigit(), f"Retry-After must be digits: {value!r}")
        seconds = int(value)
        self.assertIn(seconds, range(1, 61))
        return seconds

    def test_publish_rate_limited_with_unified_body_and_retry_after(self) -> None:
        status, _, _ = self.publish(topic="a")
        self.assertEqual(status, 202)
        status, _, _ = self.publish(topic="b")
        self.assertEqual(status, 202)
        status, error, headers = self.publish(topic="c")
        self.assertEqual(status, 429)
        self.assertEqual(error["error"]["code"], "rate_limit_exceeded")
        self.assertIn("message", error["error"])
        self.assert_retry_after(headers)

    def test_telemetry_rate_limited_with_unified_body_and_retry_after(self) -> None:
        points = [self.point(i) for i in range(5)]
        status, result, _ = self.submit("r1", points)
        self.assertEqual(status, 202)
        self.assertEqual(result["accepted_count"], 5)
        status, error, headers = self.submit("r2", [self.point(5)])
        self.assertEqual(status, 429)
        self.assertEqual(error["error"]["code"], "rate_limit_exceeded")
        self.assert_retry_after(headers)

    def test_telemetry_batch_over_quota_is_all_or_nothing(self) -> None:
        self.submit("r1", [self.point(0), self.point(1), self.point(2)])
        # 剩余 2 个点，整批 3 个点被 429 拒绝。
        status, _, headers = self.submit(
            "r2", [self.point(3), self.point(4), self.point(5)]
        )
        self.assertEqual(status, 429)
        self.assert_retry_after(headers)
        # 未产生幂等记录：同 request_id 的 2 点批次作为新请求受理。
        status, result, _ = self.submit("r2", [self.point(6), self.point(7)])
        self.assertEqual(status, 202)
        self.assertEqual(result["accepted_count"], 2)

    def test_idempotent_replay_succeeds_after_quota_exhausted(self) -> None:
        points = [self.point(0), self.point(1)]
        status, first, _ = self.submit("r1", points)
        self.assertEqual(status, 202)
        self.submit("r2", [self.point(2), self.point(3), self.point(4)])
        # 额度耗尽：新请求 429。
        status, _, _ = self.submit("r3", [self.point(5)])
        self.assertEqual(status, 429)
        # 同 request_id 同内容重试仍返回原 202。
        status, replay, _ = self.submit("r1", points)
        self.assertEqual(status, 202)
        self.assertEqual(replay, first)

    def test_conflict_takes_priority_over_rate_limit(self) -> None:
        self.submit("r1", [self.point(0)])
        self.submit("r2", [self.point(1), self.point(2), self.point(3),
                           self.point(4)])
        # 同 request_id 内容不同：409 优先于 429。
        status, error, headers = self.submit("r1", [
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
             "value": 9}
        ])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "telemetry_request_conflict")
        self.assertIsNone(headers.get("Retry-After"))

    def test_rate_limited_publish_has_no_side_effects(self) -> None:
        # 二号设备在线订阅 data，并建立同设备规则。
        self.call(
            "POST",
            f"/v1/device-sessions/{self.other_session['session_id']}/subscriptions",
            {"session_token": self.other_session["session_token"],
             "topic_filter": "data"},
        )
        status, rule, _ = self.call("POST", "/v1/rules", {
            "rule_id": "r1",
            "topic_filter": "data",
            "enabled": True,
            "condition": {"path": ["go"], "operator": "eq", "value": True},
            "action": {"topic": "data", "payload": {"x": 1}, "qos": 0},
        })
        self.assertEqual(status, 201)
        # 用不命中的主题占满一号设备发布额度。
        self.publish(topic="z")
        self.publish(topic="z")
        # 命中订阅与规则的发布被限流。
        status, error, headers = self.publish(
            topic="data", payload={"go": True}
        )
        self.assertEqual(status, 429)
        self.assertEqual(error["error"]["code"], "rate_limit_exceeded")
        self.assert_retry_after(headers)
        # 二号设备拉不到原消息或规则动作。
        status, polled, _ = self.call(
            "POST",
            f"/v1/device-sessions/{self.other_session['session_id']}/messages/poll",
            {"session_token": self.other_session["session_token"],
             "max_messages": 100},
        )
        self.assertEqual(status, 200)
        self.assertEqual(polled["messages"], [])

    def test_quota_isolated_per_device(self) -> None:
        self.publish(topic="a")
        self.publish(topic="b")
        status, _, _ = self.publish(topic="c")
        self.assertEqual(status, 429)
        # 另一台设备仍有完整额度。
        status, _, _ = self.publish(session=self.other_session, topic="a")
        self.assertEqual(status, 202)
        status, _, _ = self.publish(session=self.other_session, topic="b")
        self.assertEqual(status, 202)
        status, _, headers = self.publish(session=self.other_session,
                                          topic="c")
        self.assertEqual(status, 429)
        self.assert_retry_after(headers)

    def test_validation_and_auth_failures_are_not_limited(self) -> None:
        # 400/401 等既有失败不携带 Retry-After，且不消耗额度。
        status, error, headers = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/publish",
            {"session_token": self.session["session_token"], "topic": "",
             "payload": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_request")
        self.assertIsNone(headers.get("Retry-After"))
        status, _, _ = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/publish",
            {"session_token": "wrong", "topic": "t", "payload": 1},
        )
        self.assertEqual(status, 401)
        # 两类失败均未占用额度：仍可成功发布两次。
        self.assertEqual(self.publish(topic="ok-1")[0], 202)
        self.assertEqual(self.publish(topic="ok-2")[0], 202)
        self.assertEqual(self.publish(topic="ok-3")[0], 429)

    def test_non_limited_endpoints_unaffected(self) -> None:
        # 发布额度耗尽不影响其他入口。
        self.publish(topic="a")
        self.publish(topic="b")
        status, _, _ = self.publish(topic="c")
        self.assertEqual(status, 429)
        # 健康检查。
        status, health, _ = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        # 消息拉取入口。
        status, polled, _ = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/messages/poll",
            {"session_token": self.session["session_token"],
             "max_messages": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(polled["messages"], [])
        # 命令下发入口（非发布/遥测，不受两个限流约束）。
        status, command, _ = self.call(
            "POST", "/v1/devices/sensor-1/commands",
            {"command_name": "reboot", "payload": {}, "ttl_seconds": 60},
        )
        self.assertEqual(status, 202)
        self.assertIn("command_id", command)
        # 遥测额度此前未被发布消耗：5 个点正常受理。
        status, result, _ = self.submit("t1", [self.point(i) for i in range(5)])
        self.assertEqual(status, 202)
        self.assertEqual(result["accepted_count"], 5)


if __name__ == "__main__":
    unittest.main()
