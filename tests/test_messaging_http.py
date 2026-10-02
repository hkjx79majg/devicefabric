import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class MessagingHttpTest(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None, raw_body: bytes | None = None):
        data = raw_body
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

    def connect(self, client_id="cli-1", keepalive=30):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })
        self.assertEqual(status, 201)
        return session

    def subscribe(self, session, topic_filter):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/subscriptions",
            {"session_token": session["session_token"], "topic_filter": topic_filter},
        )

    def publish(self, session, topic, payload=None):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish",
            {"session_token": session["session_token"], "topic": topic,
             "payload": payload},
        )

    def poll(self, session, max_messages=100):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"], "max_messages": max_messages},
        )

    def test_subscribe_publish_poll_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        subscriber = self.connect(client_id="sub")

        status, payload = self.subscribe(subscriber, "factory/#")
        self.assertEqual(status, 200)
        self.assertEqual(payload["subscriptions"], ["factory/#"])

        # 重复订阅幂等。
        status, payload = self.subscribe(subscriber, "factory/#")
        self.assertEqual(status, 200)
        self.assertEqual(payload["subscriptions"], ["factory/#"])

        status, payload = self.publish(publisher, "factory/line1", {"temp": 21})
        self.assertEqual(status, 202)
        self.assertEqual(payload["matched_count"], 1)
        self.assertTrue(payload["message_id"])
        message_id = payload["message_id"]

        status, payload = self.poll(subscriber, max_messages=10)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["messages"]), 1)
        message = payload["messages"][0]
        self.assertEqual(message["message_id"], message_id)
        self.assertEqual(message["topic"], "factory/line1")
        self.assertEqual(message["payload"], {"temp": 21})
        self.assertEqual(message["publisher_device_id"], "sensor-1")
        self.assertTrue(message["published_at"].endswith("Z"))

        # 队列已排空。
        status, payload = self.poll(subscriber)
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"], [])

    def test_error_codes_over_http(self) -> None:
        session = self.connect()
        sid = session["session_id"]
        token = session["session_token"]

        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/subscriptions",
            {"session_token": "x", "topic_filter": "a"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 令牌错误。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/publish",
            {"session_token": "wrong", "topic": "a", "payload": None},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # 会话已关闭。
        replacement = self.connect(client_id="cli-1")
        self.assertNotEqual(replacement["session_id"], sid)
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/poll",
            {"session_token": token, "max_messages": 1},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    def test_invalid_requests_over_http(self) -> None:
        session = self.connect()
        sid = session["session_id"]
        token = session["session_token"]

        # 非 JSON 请求体。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/subscriptions", raw_body=b"not json{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 非法过滤器。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/subscriptions",
            {"session_token": token, "topic_filter": "a/#/b"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 主题含通配符。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/publish",
            {"session_token": token, "topic": "a/+", "payload": None},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # max_messages 越界。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/poll",
            {"session_token": token, "max_messages": 0},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 多余字段。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/poll",
            {"session_token": token, "max_messages": 1, "extra": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
