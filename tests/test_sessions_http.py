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

    def connect(self, client_id="cli-1", keepalive=30, credential=None):
        return self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def test_connect_heartbeat_and_snapshot_flow(self) -> None:
        status, session = self.connect()
        self.assertEqual(status, 201)
        self.assertTrue(session["session_id"])
        self.assertTrue(session["session_token"])
        self.assertEqual(session["device_id"], "sensor-1")
        self.assertEqual(session["client_id"], "cli-1")
        self.assertTrue(session["online"])

        sid = session["session_id"]
        status, snapshot = self.call("GET", f"/v1/device-sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertNotIn("session_token", snapshot)
        self.assertEqual(snapshot["session_id"], sid)

        status, refreshed = self.call(
            "POST", f"/v1/device-sessions/{sid}/heartbeat",
            {"session_token": session["session_token"]},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("session_token", refreshed)
        self.assertTrue(refreshed["online"])

    def test_reconnect_replaces_old_session(self) -> None:
        _, first = self.connect()
        status, second = self.connect()
        self.assertEqual(status, 201)
        self.assertNotEqual(first["session_id"], second["session_id"])

        status, old = self.call("GET", f"/v1/device-sessions/{first['session_id']}")
        self.assertEqual(status, 200)
        self.assertFalse(old["online"])
        self.assertEqual(old["state"], "closed")
        self.assertEqual(old["reason"], "replaced")

        status, payload = self.call(
            "POST", f"/v1/device-sessions/{first['session_id']}/heartbeat",
            {"session_token": first["session_token"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    def test_revoke_closes_sessions_over_http(self) -> None:
        _, session = self.connect()
        status, revoked = self.call("POST", "/v1/devices/sensor-1/revoke")
        self.assertEqual(status, 200)
        self.assertFalse(revoked["active"])

        status, snapshot = self.call("GET", f"/v1/device-sessions/{session['session_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["state"], "closed")
        self.assertEqual(snapshot["reason"], "device_revoked")

    def test_connect_credential_failures(self) -> None:
        status, payload = self.connect(credential="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

        status, payload = self.call("POST", "/v1/device-sessions", {
            "device_id": "ghost",
            "credential": "x",
            "client_id": "c",
            "keepalive_seconds": 5,
        })
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

    def test_session_error_codes_over_http(self) -> None:
        _, session = self.connect()
        sid = session["session_id"]

        status, payload = self.call("GET", "/v1/device-sessions/unknown-session")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/heartbeat", {"session_token": "nope"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

    def test_invalid_requests_over_http(self) -> None:
        # 非 JSON 请求体。
        status, payload = self.call("POST", "/v1/device-sessions", raw_body=b"not json{")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 缺少必填字段。
        status, payload = self.call("POST", "/v1/device-sessions", {"device_id": "sensor-1"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 未知字段。
        status, payload = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": "c",
            "keepalive_seconds": 5,
            "extra": 1,
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # keepalive 越界。
        status, payload = self.connect(keepalive=3601)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 心跳缺少 token。
        _, session = self.connect()
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/heartbeat", {}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
