import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class SessionHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

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

    def register(self, device_id="sensor-1"):
        status, created = self.call(
            "POST", "/v1/devices",
            {"device_id": device_id, "display_name": device_id},
        )
        self.assertEqual(status, 201)
        return created

    def connect(self, credential, *, device_id="sensor-1", client_id="client-a",
                keepalive_seconds=300):
        return self.call(
            "POST", "/v1/device-sessions",
            {
                "device_id": device_id,
                "credential": credential,
                "client_id": client_id,
                "keepalive_seconds": keepalive_seconds,
            },
        )

    def test_create_get_heartbeat_lifecycle(self) -> None:
        credential = self.register()["credential"]

        status, session = self.connect(credential, keepalive_seconds=60)
        self.assertEqual(status, 201)
        self.assertTrue(session["session_id"])
        self.assertTrue(session["session_token"])
        self.assertEqual(session["device_id"], "sensor-1")
        self.assertEqual(session["client_id"], "client-a")
        self.assertTrue(session["online"])
        self.assertIn("connected_at", session)
        self.assertEqual(session["connected_at"], session["last_seen_at"])
        self.assertIn("expires_at", session)

        session_id = session["session_id"]
        token = session["session_token"]

        status, snapshot = self.call("GET", f"/v1/device-sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("session_token", snapshot)
        self.assertEqual(snapshot["session_id"], session_id)
        self.assertTrue(snapshot["online"])

        status, beat = self.call(
            "POST", f"/v1/device-sessions/{session_id}/heartbeat",
            {"session_token": token},
        )
        self.assertEqual(status, 200)
        self.assertTrue(beat["online"])
        self.assertNotIn("session_token", beat)
        self.assertGreaterEqual(beat["last_seen_at"], snapshot["last_seen_at"])

    def test_reconnect_replaces_old_session(self) -> None:
        credential = self.register()["credential"]
        _, first = self.connect(credential)
        _, second = self.connect(credential)
        self.assertNotEqual(first["session_id"], second["session_id"])

        status, old = self.call("GET", f"/v1/device-sessions/{first['session_id']}")
        self.assertEqual(status, 200)
        self.assertFalse(old["online"])
        self.assertEqual(old["status"], "closed")
        self.assertEqual(old["reason"], "replaced")

        status, payload = self.call(
            "POST", f"/v1/device-sessions/{first['session_id']}/heartbeat",
            {"session_token": first["session_token"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    def test_create_errors(self) -> None:
        credential = self.register()["credential"]

        # 非法 JSON。
        status, payload = self.call(
            "POST", "/v1/device-sessions", raw_body=b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 非对象。
        status, payload = self.call("POST", "/v1/device-sessions", body=[1, 2])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 缺字段 / 未知字段 / 越界。
        for body in (
            {},
            {"device_id": "sensor-1", "credential": credential,
             "client_id": "client-a"},
            {"device_id": "sensor-1", "credential": credential,
             "client_id": "client-a", "keepalive_seconds": 300, "x": 1},
            {"device_id": "sensor-1", "credential": credential,
             "client_id": "bad client", "keepalive_seconds": 300},
            {"device_id": "sensor-1", "credential": credential,
             "client_id": "client-a", "keepalive_seconds": 4},
            {"device_id": "sensor-1", "credential": credential,
             "client_id": "client-a", "keepalive_seconds": 3601},
        ):
            with self.subTest(body=body):
                status, payload = self.call("POST", "/v1/device-sessions", body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        # 设备不存在 / 凭据错误。
        status, payload = self.connect("nope")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")
        status, payload = self.connect(credential, device_id="ghost")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

        # 凭据已轮换。
        _, rotated = self.call("POST", "/v1/devices/sensor-1/credential/rotate")
        status, payload = self.connect(credential)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

        # 设备已吊销。
        self.call("POST", "/v1/devices/sensor-1/revoke")
        status, payload = self.connect(rotated["credential"])
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

    def test_session_snapshot_and_heartbeat_errors(self) -> None:
        credential = self.register()["credential"]
        _, session = self.connect(credential)
        sid = session["session_id"]

        # 未知会话：GET 404，心跳 404。
        status, payload = self.call("GET", "/v1/device-sessions/unknown-id")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")
        status, payload = self.call(
            "POST", "/v1/device-sessions/unknown-id/heartbeat",
            {"session_token": "x"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # token 不匹配；错误体不得泄露真实令牌。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/heartbeat",
            {"session_token": "wrong-token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")
        self.assertNotIn(session["session_token"], json.dumps(payload))
        self.assertEqual(set(payload["error"]), {"code", "message"})

        # 心跳请求体非法。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/heartbeat", raw_body=b"bad"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_revoke_closes_session_but_response_unchanged(self) -> None:
        credential = self.register()["credential"]
        _, session = self.connect(credential)
        sid = session["session_id"]

        status, revoked = self.call("POST", "/v1/devices/sensor-1/revoke")
        self.assertEqual(status, 200)
        self.assertFalse(revoked["active"])
        self.assertNotIn("credential", revoked)

        # 吊销响应幂等不变。
        status, revoked_again = self.call("POST", "/v1/devices/sensor-1/revoke")
        self.assertEqual(status, 200)
        self.assertEqual(revoked_again["credential_version"], 1)

        status, snapshot = self.call("GET", f"/v1/device-sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["status"], "closed")
        self.assertEqual(snapshot["reason"], "device_revoked")

        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/heartbeat",
            {"session_token": session["session_token"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")


if __name__ == "__main__":
    unittest.main()
