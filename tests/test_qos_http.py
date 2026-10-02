import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class QosHttpTest(unittest.TestCase):
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

    def publish(self, session, topic, payload, qos=None):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload}
        if qos is not None:
            body["qos"] = qos
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish", body,
        )

    def poll(self, session, max_messages=100):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def ack(self, session, delivery_ids, token=None):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/ack",
            {"session_token": token if token is not None
             else session["session_token"],
             "delivery_ids": delivery_ids},
        )

    def test_qos1_publish_poll_ack_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        status, _ = self.subscribe(listener, "house/#")
        self.assertEqual(status, 200)

        status, body = self.publish(publisher, "house/room1", {"v": 1}, qos=1)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 1)
        message_id = body["message_id"]

        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)
        message = body["messages"][0]
        self.assertEqual(message["message_id"], message_id)
        self.assertEqual(message["topic"], "house/room1")
        self.assertEqual(message["payload"], {"v": 1})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertEqual(message["qos"], 1)
        self.assertTrue(message["delivery_id"])
        self.assertIs(message["dup"], False)

        # 确认前重投：同一 delivery_id 与 message_id，dup 为 true。
        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        redelivered = body["messages"][0]
        self.assertEqual(redelivered["delivery_id"], message["delivery_id"])
        self.assertEqual(redelivered["message_id"], message_id)
        self.assertIs(redelivered["dup"], True)

        status, body = self.ack(listener, [message["delivery_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"acked_count": 1})

        # 重复确认幂等。
        status, body = self.ack(listener, [message["delivery_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"acked_count": 0})

        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_publish_without_qos_keeps_qos0_shape(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        status, _ = self.publish(publisher, "t", 1)
        self.assertEqual(status, 202)
        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        message = body["messages"][0]
        self.assertEqual(
            set(message),
            {"message_id", "topic", "payload", "publisher_device_id",
             "published_at"},
        )
        status, body = self.poll(listener)
        self.assertEqual(body["messages"], [])

    def test_publish_invalid_qos_returns_400(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        for qos in (2, -1, "1", 1.5, True):
            with self.subTest(qos=qos):
                status, payload = self.publish(publisher, "t", 1, qos=qos)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        status, body = self.poll(listener)
        self.assertEqual(body["messages"], [])

    def test_ack_error_codes_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        self.publish(publisher, "t", 1, qos=1)
        _, body = self.poll(listener)
        delivery_id = body["messages"][0]["delivery_id"]
        sid = listener["session_id"]

        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/messages/ack",
            {"session_token": "x", "delivery_ids": [delivery_id]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 令牌错误。
        status, payload = self.ack(listener, [delivery_id], token="bad")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # 从未属于该会话的标识：整体不确认。
        status, payload = self.ack(listener, [delivery_id, "ghost-delivery"])
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "delivery_not_found")
        status, body = self.poll(listener)
        self.assertEqual(body["messages"][0]["delivery_id"], delivery_id)

        # 请求体不合法。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/ack",
            {"session_token": listener["session_token"], "delivery_ids": []},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/ack", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 会话被取代后确认返回 409。
        replacement = self.connect(device_id="dev-b", client_id="sub")
        status, payload = self.ack(listener, [delivery_id])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")
        # 新会话不继承确认历史。
        status, payload = self.ack(replacement, [delivery_id])
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "delivery_not_found")


if __name__ == "__main__":
    unittest.main()
