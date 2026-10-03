import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class ShadowHttpTest(unittest.TestCase):
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
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })
        self.assertEqual(status, 201)
        return session

    def test_initial_shadow_snapshot_over_http(self) -> None:
        status, shadow = self.call("GET", "/v1/devices/sensor-1/shadow")
        self.assertEqual(status, 200)
        self.assertEqual(shadow, {
            "device_id": "sensor-1",
            "version": 0,
            "desired": {},
            "reported": {},
            "delta": {},
            "updated_at": None,
        })

    def test_get_unknown_device_shadow_is_not_found(self) -> None:
        status, payload = self.call("GET", "/v1/devices/ghost/shadow")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "device_not_found")

    def test_set_desired_flow_over_http(self) -> None:
        status, shadow = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired",
            {"state": {"power": "on", "cfg": {"level": 3}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["desired"], {"power": "on", "cfg": {"level": 3}})
        self.assertEqual(shadow["delta"], {"power": "on", "cfg": {"level": 3}})
        self.assertIsInstance(shadow["updated_at"], str)
        self.assertTrue(shadow["updated_at"].endswith("Z"))

        # 再次读取为持久快照。
        status, fetched = self.call("GET", "/v1/devices/sensor-1/shadow")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, shadow)

    def test_set_desired_with_expected_version(self) -> None:
        status, shadow = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired",
            {"state": {"a": 1}, "expected_version": 0},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 1)

        status, payload = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired",
            {"state": {"b": 2}, "expected_version": 0},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "shadow_version_conflict")

        status, shadow = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired",
            {"state": {"b": 2}, "expected_version": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 2)

    def test_set_desired_unknown_device_is_not_found(self) -> None:
        status, payload = self.call(
            "PUT", "/v1/devices/ghost/shadow/desired", {"state": {}}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "device_not_found")

    def test_report_flow_and_delta_over_http(self) -> None:
        session = self.connect()
        self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired",
            {"state": {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}},
        )
        status, shadow = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": session["session_token"],
             "state": {"a": 1, "nested": {"x": 1, "y": 9}, "extra": 0}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 2)
        self.assertEqual(shadow["reported"],
                         {"a": 1, "nested": {"x": 1, "y": 9}, "extra": 0})
        # a 一致；b 在 reported 中缺失；nested.x 一致、nested.y 不同；
        # reported 独有的 extra 被忽略。
        self.assertEqual(shadow["delta"], {"b": 2, "nested": {"y": 2}})

    def test_report_expected_version_conflict(self) -> None:
        session = self.connect()
        self.call("PUT", "/v1/devices/sensor-1/shadow/desired", {"state": {"a": 1}})
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": session["session_token"], "state": {"a": 1},
             "expected_version": 0},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "shadow_version_conflict")
        status, shadow = self.call("GET", "/v1/devices/sensor-1/shadow")
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["reported"], {})

    def test_report_session_error_codes_over_http(self) -> None:
        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/unknown/shadow/reported",
            {"session_token": "x", "state": {}},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 错误令牌。
        session = self.connect()
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": "wrong", "state": {}},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # 关闭会话。
        self.connect()  # 取代旧会话
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": session["session_token"], "state": {}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    def test_revoked_device_desired_still_writable_reported_not(self) -> None:
        session = self.connect()
        status, _ = self.call("POST", "/v1/devices/sensor-1/revoke")
        self.assertEqual(status, 200)

        status, shadow = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired", {"state": {"a": 1}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["desired"], {"a": 1})

        status, shadow = self.call("GET", "/v1/devices/sensor-1/shadow")
        self.assertEqual(status, 200)
        self.assertEqual(shadow["desired"], {"a": 1})

        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": session["session_token"], "state": {"a": 1}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    def test_shadow_invalid_requests_over_http(self) -> None:
        # 非 JSON 请求体。
        status, payload = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/desired", raw_body=b"not json{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        for body in (
            {},
            {"state": {}, "extra": 1},
            {"state": []},
            {"state": "x"},
            {"state": {}, "expected_version": -1},
            {"state": {}, "expected_version": "1"},
        ):
            with self.subTest(body=body):
                status, payload = self.call(
                    "PUT", "/v1/devices/sensor-1/shadow/desired", body
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        session = self.connect()
        for body in (
            {},
            {"state": {}},
            {"session_token": session["session_token"]},
            {"session_token": session["session_token"], "state": {}, "x": 1},
            {"session_token": session["session_token"], "state": 5},
            {"session_token": 9, "state": {}},
            {"session_token": session["session_token"], "state": {},
             "expected_version": True},
        ):
            with self.subTest(body=body):
                status, payload = self.call(
                    "POST",
                    f"/v1/device-sessions/{session['session_id']}/shadow/reported",
                    body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        # 所有失败请求均不改变影子。
        status, shadow = self.call("GET", "/v1/devices/sensor-1/shadow")
        self.assertEqual(shadow["version"], 0)
        self.assertEqual(shadow["updated_at"], None)


if __name__ == "__main__":
    unittest.main()
