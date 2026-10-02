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
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
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

    def publish(self, session, topic, payload, retain=None, qos=None):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload}
        if retain is not None:
            body["retain"] = retain
        if qos is not None:
            body["qos"] = qos
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish", body
        )

    def poll(self, session):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"], "max_messages": 100},
        )

    def test_retain_publish_then_subscribe_replays_over_http(self) -> None:
        publisher = self.connect(client_id="pub")

        status, body = self.publish(publisher, "house/r1/temp", {"v": 21},
                                    retain=True)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 0)
        message_id = body["message_id"]

        listener = self.connect(device_id="dev-b", client_id="sub")
        status, body = self.subscribe(listener, "house/+/temp")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"topic_filter": "house/+/temp"})

        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)
        message = body["messages"][0]
        self.assertEqual(message["message_id"], message_id)
        self.assertEqual(message["topic"], "house/r1/temp")
        self.assertEqual(message["payload"], {"v": 21})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertIs(message["retained"], True)
        self.assertNotIn("qos", message)

    def test_qos1_retained_replay_and_ack_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1, retain=True)
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        status, body = self.poll(listener)
        self.assertEqual(status, 200)
        message = body["messages"][0]
        self.assertEqual(message["qos"], 1)
        self.assertIs(message["dup"], False)
        self.assertIs(message["retained"], True)
        delivery_id = message["delivery_id"]

        # 重投 dup=true。
        status, body = self.poll(listener)
        self.assertEqual(body["messages"][0]["dup"], True)
        self.assertEqual(body["messages"][0]["delivery_id"], delivery_id)

        status, body = self.call(
            "POST",
            f"/v1/device-sessions/{listener['session_id']}/messages/ack",
            {"session_token": listener["session_token"],
             "delivery_ids": [delivery_id]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"acked_count": 1})
        status, body = self.poll(listener)
        self.assertEqual(body["messages"], [])

    def test_retained_clear_then_replay_empty(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        status, body = self.publish(publisher, "t", None, retain=True)
        self.assertEqual(status, 202)
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        status, body = self.poll(listener)
        self.assertEqual(body["messages"], [])

    def test_invalid_retain_rejected_over_http(self) -> None:
        session = self.connect()
        for value in (1, 0, "true", None, [True]):
            with self.subTest(value=value):
                status, payload = self.call(
                    "POST",
                    f"/v1/device-sessions/{session['session_id']}/publish",
                    {"session_token": session["session_token"], "topic": "t",
                     "payload": 1, "retain": value},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_realtime_message_has_no_retained_field_over_http(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="dev-b", client_id="sub")
        self.subscribe(listener, "t")
        status, body = self.publish(publisher, "t", 1, retain=True)
        self.assertEqual(status, 202)
        self.assertEqual(body["matched_count"], 1)
        status, body = self.poll(listener)
        self.assertNotIn("retained", body["messages"][0])


if __name__ == "__main__":
    unittest.main()
