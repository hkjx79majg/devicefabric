import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class RoutingHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.devices = {}
        for device_id, name in (("dev-a", "甲"), ("dev-b", "乙")):
            status, device = self.call(
                "POST", "/v1/devices",
                {"device_id": device_id, "display_name": name},
            )
            self.assertEqual(status, 201)
            self.devices[device_id] = device

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
        req = urllib_request.Request(self.url(path), data=data, headers=headers,
                                     method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def connect(self, device_id="dev-a", client_id="cli-1", keepalive=30):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": device_id,
            "credential": self.devices[device_id]["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })
        self.assertEqual(status, 201)
        return session

    def subscribe(self, session, topic_filter):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/subscriptions",
            {"session_token": session["session_token"],
             "topic_filter": topic_filter},
        )

    def publish(self, session, topic, payload):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish",
            {"session_token": session["session_token"], "topic": topic,
             "payload": payload},
        )

    def poll(self, session, max_messages=100):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def test_subscribe_publish_poll_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")

        status, body = self.subscribe(listener, "house/+/temp/#")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"topic_filter": "house/+/temp/#"})

        # 重复订阅幂等。
        status, _ = self.subscribe(listener, "house/+/temp/#")
        self.assertEqual(status, 200)

        status, body = self.publish(publisher, "house/room1/temp/attic",
                                    {"v": 21.5, "tags": ["x"]})
        self.assertEqual(status, 202)
        self.assertIn("message_id", body)
        self.assertEqual(body["matched_count"], 1)

        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)
        message = body["messages"][0]
        self.assertEqual(message["message_id"], body["messages"][0]["message_id"])
        self.assertEqual(message["topic"], "house/room1/temp/attic")
        self.assertEqual(message["payload"], {"v": 21.5, "tags": ["x"]})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertRegex(message["published_at"],
                         r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")

        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_publish_to_self_and_single_enqueue(self) -> None:
        session = self.connect()
        self.subscribe(session, "a/#")
        self.subscribe(session, "a/b")
        status, body = self.publish(session, "a/b", None)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 1)
        status, body = self.poll(session, max_messages=1)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)

    def test_error_codes_over_http(self) -> None:
        session = self.connect()
        sid = session["session_id"]

        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/subscriptions",
            {"session_token": "x", "topic_filter": "a"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 令牌错误（publish）。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/publish",
            {"session_token": "bad", "topic": "a", "payload": 1},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # closed 会话冲突（poll）。
        replacement = self.connect()
        status, payload = self.poll(session)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")
        # 新会话队列不受影响。
        status, body = self.poll(replacement)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_invalid_requests_over_http(self) -> None:
        session = self.connect()
        sid = session["session_id"]

        # 非 JSON 请求体。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/subscriptions", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 订阅非法过滤器。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/subscriptions",
            {"session_token": session["session_token"], "topic_filter": "a/"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 发布缺字段 / 主题含通配符。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/publish",
            {"session_token": session["session_token"], "topic": "a"},
        )
        self.assertEqual(status, 400)
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/publish",
            {"session_token": session["session_token"], "topic": "a/+",
             "payload": 1},
        )
        self.assertEqual(status, 400)

        # poll 多余字段与 max_messages 越界。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/poll",
            {"session_token": session["session_token"], "max_messages": 0},
        )
        self.assertEqual(status, 400)
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/poll",
            {"session_token": session["session_token"], "max_messages": 101},
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
