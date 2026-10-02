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

    def ack(self, session, delivery_ids):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/ack",
            {"session_token": session["session_token"],
             "delivery_ids": delivery_ids},
        )

    def test_qos1_publish_poll_ack_flow(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        status, _ = self.subscribe(listener, "house/#")
        self.assertEqual(status, 200)

        status, body = self.publish(publisher, "house/room1", {"v": 21.5}, qos=1)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 1)
        self.assertIn("message_id", body)

        status, polled = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertEqual(message["message_id"], body["message_id"])
        self.assertEqual(message["topic"], "house/room1")
        self.assertEqual(message["payload"], {"v": 21.5})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertEqual(message["qos"], 1)
        self.assertTrue(message["delivery_id"])
        self.assertIs(message["dup"], False)

        # 确认前重投，dup 为 true，message_id 与 delivery_id 不变。
        status, polled = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        self.assertIs(polled["messages"][0]["dup"], True)
        self.assertEqual(polled["messages"][0]["delivery_id"],
                         message["delivery_id"])
        self.assertEqual(polled["messages"][0]["message_id"],
                         message["message_id"])

        status, acked = self.ack(listener, [message["delivery_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(acked, {"acked_count": 1})

        # 重复确认幂等，队列不再重投。
        status, acked = self.ack(listener, [message["delivery_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(acked, {"acked_count": 0})
        status, polled = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(polled["messages"], [])

    def test_publish_without_qos_keeps_qos0_shape(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        status, body = self.publish(publisher, "t", 1)
        self.assertEqual(status, 202)
        status, polled = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertNotIn("qos", message)
        self.assertNotIn("delivery_id", message)
        self.assertNotIn("dup", message)
        status, polled = self.poll(listener)
        self.assertEqual(polled["messages"], [])

    def test_invalid_qos_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        for qos in (2, "1", 1.0, True, None):
            with self.subTest(qos=qos):
                status, payload = self.call(
                    "POST",
                    f"/v1/device-sessions/{publisher['session_id']}/publish",
                    {"session_token": publisher["session_token"], "topic": "t",
                     "payload": 1, "qos": qos},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        status, polled = self.poll(listener)
        self.assertEqual(polled["messages"], [])

    def test_ack_error_codes_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        self.publish(publisher, "t", 1, qos=1)
        _, polled = self.poll(listener)
        delivery_id = polled["messages"][0]["delivery_id"]
        sid = listener["session_id"]

        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/messages/ack",
            {"session_token": "x", "delivery_ids": [delivery_id]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 令牌错误。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/messages/ack",
            {"session_token": "bad", "delivery_ids": [delivery_id]},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # 从未属于该会话的标识：整体不确认。
        status, payload = self.ack(listener, [delivery_id, "ghost-delivery"])
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "delivery_not_found")
        _, polled = self.poll(listener)
        self.assertEqual(len(polled["messages"]), 1)

        # 非法请求体。
        for body in (
            {"session_token": listener["session_token"], "delivery_ids": []},
            {"session_token": listener["session_token"],
             "delivery_ids": [delivery_id, delivery_id]},
            {"session_token": listener["session_token"], "delivery_ids": "x"},
            {"session_token": listener["session_token"]},
        ):
            status, payload = self.call(
                "POST", f"/v1/device-sessions/{sid}/messages/ack", body,
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_request")

        # 非在线会话。
        replacement = self.connect(device_id="dev-b", client_id="sub")
        status, payload = self.ack(listener, [delivery_id])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")
        # 新会话不继承旧会话的投递标识。
        status, payload = self.ack(replacement, [delivery_id])
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "delivery_not_found")


if __name__ == "__main__":
    unittest.main()
