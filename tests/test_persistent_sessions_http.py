import json
import threading
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service, _utc_now


class PersistentSessionHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        handler_cls = type("TestHandler", (Handler,), {"service": self.service})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, self.pub_device = self.call(
            "POST", "/v1/devices", {"device_id": "pub", "display_name": "发布者"}
        )
        self.assertEqual(status, 201)
        status, self.sub_device = self.call(
            "POST", "/v1/devices", {"device_id": "sub", "display_name": "订阅者"}
        )
        self.assertEqual(status, 201)

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

    def connect(self, device, client_id, clean_start=None):
        body = {
            "device_id": device["device_id"],
            "credential": device["credential"],
            "client_id": client_id,
            "keepalive_seconds": 30,
        }
        if clean_start is not None:
            body["clean_start"] = clean_start
        return self.call("POST", "/v1/device-sessions", body)

    def post(self, sid, suffix, body):
        return self.call("POST", f"/v1/device-sessions/{sid}{suffix}", body)

    def test_offline_delivery_flow_over_http(self) -> None:
        _, first = self.connect(self.sub_device, "cli", clean_start=False)
        self.assertEqual(
            self.post(first["session_id"], "/subscriptions",
                      {"session_token": first["session_token"], "topic_filter": "t"})[0],
            200,
        )
        # 让持久会话心跳超时（状态在下一次路由时惰性转为 expired）。
        record = self.service._sessions[first["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

        _, publisher = self.connect(self.pub_device, "pub")
        status, pub = self.post(
            publisher["session_id"], "/publish",
            {"session_token": publisher["session_token"], "topic": "t",
             "payload": {"v": 1}, "qos": 1},
        )
        self.assertEqual(status, 202)
        self.assertEqual(pub["matched_count"], 0)

        _, second = self.connect(self.sub_device, "cli", clean_start=False)
        status, polled = self.post(
            second["session_id"], "/messages/poll",
            {"session_token": second["session_token"], "max_messages": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertEqual(message["payload"], {"v": 1})
        self.assertIs(message["dup"], False)

        status, acked = self.post(
            second["session_id"], "/messages/ack",
            {"session_token": second["session_token"],
             "delivery_ids": [message["delivery_id"]]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(acked["acked_count"], 1)

    def test_non_boolean_clean_start_is_400_and_keeps_old_session(self) -> None:
        _, session = self.connect(self.sub_device, "cli", clean_start=False)
        self.post(session["session_id"], "/subscriptions",
                  {"session_token": session["session_token"], "topic_filter": "t"})
        body = {
            "device_id": "sub",
            "credential": self.sub_device["credential"],
            "client_id": "cli",
            "keepalive_seconds": 30,
        }
        for bad in ("false", 1, 0, None, [], {}):
            bad_body = dict(body, clean_start=bad)
            status, payload = self.call("POST", "/v1/device-sessions", bad_body)
            self.assertEqual(status, 400, bad)
            self.assertEqual(payload["error"]["code"], "invalid_request")
        # 旧会话仍在线。
        status, snapshot = self.call(
            "GET", f"/v1/device-sessions/{session['session_id']}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(snapshot["online"])

    def test_clean_start_true_then_false_are_accepted(self) -> None:
        status, _ = self.connect(self.sub_device, "cli-a", clean_start=True)
        self.assertEqual(status, 201)
        status, _ = self.connect(self.sub_device, "cli-b", clean_start=False)
        self.assertEqual(status, 201)
        status, _ = self.connect(self.sub_device, "cli-c")  # 缺省
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
