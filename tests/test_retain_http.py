import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class RetainHttpTest(unittest.TestCase):
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

    def call(self, method: str, path: str, body=None):
        data = None
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

    def publish(self, session, topic, payload, **extra):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload}
        body.update(extra)
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish", body,
        )

    def poll(self, session, max_messages=100):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def ack(self, session, delivery_ids):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/ack",
            {"session_token": session["session_token"],
             "delivery_ids": delivery_ids},
        )

    def test_retain_publish_replay_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        status, body = self.publish(publisher, "house/room1", {"v": 21.5},
                                    retain=True)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 0)
        self.assertIn("message_id", body)

        listener = self.connect(device_id="dev-b", client_id="sub")
        status, sub = self.subscribe(listener, "house/#")
        self.assertEqual(status, 200)
        self.assertEqual(sub, {"topic_filter": "house/#"})

        status, polled = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertEqual(message["message_id"], body["message_id"])
        self.assertEqual(message["topic"], "house/room1")
        self.assertEqual(message["payload"], {"v": 21.5})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertIs(message["retained"], True)
        self.assertNotIn("qos", message)
        # QoS 0 回放拉取后移除。
        status, polled = self.poll(listener)
        self.assertEqual(polled["messages"], [])

    def test_retain_null_clears_and_live_delivery_has_no_flag(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        self.publish(publisher, "t", "kept", retain=True)
        # 实时投递不含 retained 字段。
        _, polled = self.poll(listener)
        self.assertNotIn("retained", polled["messages"][0])

        status, body = self.publish(publisher, "t", None, retain=True)
        self.assertEqual(status, 202)
        _, polled = self.poll(listener)
        self.assertIsNone(polled["messages"][0]["payload"])
        self.assertNotIn("retained", polled["messages"][0])

        # 保留值已清除，新订阅不再回放。
        late = self.connect(device_id="dev-b", client_id="late")
        self.subscribe(late, "t")
        _, polled = self.poll(late)
        self.assertEqual(polled["messages"], [])

    def test_qos1_retain_replay_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        _, body = self.publish(publisher, "t", 1, qos=1, retain=True)
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")

        _, polled = self.poll(listener)
        message = polled["messages"][0]
        self.assertEqual(message["message_id"], body["message_id"])
        self.assertEqual(message["qos"], 1)
        self.assertIs(message["retained"], True)
        self.assertIs(message["dup"], False)

        _, polled = self.poll(listener)
        self.assertIs(polled["messages"][0]["dup"], True)
        self.assertEqual(polled["messages"][0]["delivery_id"],
                         message["delivery_id"])

        status, acked = self.ack(listener, [message["delivery_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(acked, {"acked_count": 1})

    def test_invalid_retain_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        for retain in (0, 1, "true", None, 1.0):
            with self.subTest(retain=retain):
                status, payload = self.call(
                    "POST",
                    f"/v1/device-sessions/{publisher['session_id']}/publish",
                    {"session_token": publisher["session_token"], "topic": "t",
                     "payload": 1, "retain": retain},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        _, polled = self.poll(listener)
        self.assertEqual(polled["messages"], [])

    def test_retain_publish_session_error_codes(self) -> None:
        publisher = self.connect(client_id="pub")
        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/publish",
            {"session_token": "x", "topic": "t", "payload": 1, "retain": True},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")
        # 令牌错误。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{publisher['session_id']}/publish",
            {"session_token": "bad", "topic": "t", "payload": 1,
             "retain": True},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")
        # 非在线会话。
        self.connect(client_id="pub")
        status, payload = self.publish(publisher, "t", 1, retain=True)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")
        # 失败的发布不保存保留值。
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        _, polled = self.poll(listener)
        self.assertEqual(polled["messages"], [])


if __name__ == "__main__":
    unittest.main()
