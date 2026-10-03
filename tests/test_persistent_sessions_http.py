"""clean_start=false 持久会话的 HTTP 入口测试。"""

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
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        _, self.device = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-1", "display_name": "传感器"}
        )
        _, self.other = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-2", "display_name": "发布者"}
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib_request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def connect(self, device, device_id, client_id, clean_start=None):
        body = {
            "device_id": device_id,
            "credential": device["credential"],
            "client_id": client_id,
            "keepalive_seconds": 30,
        }
        if clean_start is not None:
            body["clean_start"] = clean_start
        return self.call("POST", "/v1/device-sessions", body)

    def post(self, sid, suffix, body):
        return self.call(f"POST", f"/v1/device-sessions/{sid}{suffix}", body)

    def test_persistent_session_offline_queue_over_http(self) -> None:
        status, subscriber = self.connect(self.device, "sensor-1", "sub",
                                          clean_start=False)
        self.assertEqual(status, 201)
        self.post(subscriber["session_id"], "/subscriptions",
                  {"session_token": subscriber["session_token"], "topic_filter": "t"})

        # 推进旧会话的过期时间，再心跳触发 expired 转换。
        self.server.RequestHandlerClass.service._sessions[
            subscriber["session_id"]
        ]["expires_at"] = _utc_now() - timedelta(seconds=1)
        status, _ = self.post(
            subscriber["session_id"], "/heartbeat",
            {"session_token": subscriber["session_token"]},
        )
        self.assertEqual(status, 409)

        _, publisher = self.connect(self.other, "sensor-2", "pub")
        status, result = self.post(
            publisher["session_id"], "/publish",
            {"session_token": publisher["session_token"], "topic": "t",
             "payload": {"v": 1}, "qos": 1},
        )
        self.assertEqual(status, 202)
        self.assertEqual(result["matched_count"], 0)

        status, reopened = self.connect(self.device, "sensor-1", "sub",
                                        clean_start=False)
        self.assertEqual(status, 201)
        status, polled = self.post(
            reopened["session_id"], "/messages/poll",
            {"session_token": reopened["session_token"], "max_messages": 100},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        self.assertEqual(polled["messages"][0]["payload"], {"v": 1})
        self.assertIs(polled["messages"][0]["dup"], False)

    def test_non_boolean_clean_start_is_400_without_closing_old_session(self) -> None:
        _, existing = self.connect(self.device, "sensor-1", "sub",
                                   clean_start=False)
        for bad in ("false", 0, 1, None):
            status, payload = self.call("POST", "/v1/device-sessions", {
                "device_id": "sensor-1",
                "credential": self.device["credential"],
                "client_id": "sub",
                "keepalive_seconds": 30,
                "clean_start": bad,
            })
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_request")
        # 旧会话仍在线、令牌仍有效。
        status, _ = self.post(
            existing["session_id"], "/heartbeat",
            {"session_token": existing["session_token"]},
        )
        self.assertEqual(status, 200)

    def test_default_and_true_clean_start_still_temporary(self) -> None:
        status_default, default = self.connect(self.device, "sensor-1", "a")
        self.assertEqual(status_default, 201)
        status_true, explicit = self.connect(self.device, "sensor-1", "b",
                                             clean_start=True)
        self.assertEqual(status_true, 201)
        self.assertTrue(default["session_token"])
        self.assertTrue(explicit["session_token"])
        self.assertEqual(
            self.server.RequestHandlerClass.service._persistent_sessions, {}
        )


if __name__ == "__main__":
    unittest.main()
