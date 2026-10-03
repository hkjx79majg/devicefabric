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
        return self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def shadow_path(self, suffix=""):
        return f"/v1/devices/sensor-1/shadow{suffix}"

    # ------------------------------------------------------------------
    # 读取与初始快照
    # ------------------------------------------------------------------

    def test_get_initial_shadow_snapshot(self) -> None:
        status, shadow = self.call("GET", self.shadow_path())
        self.assertEqual(status, 200)
        self.assertEqual(shadow["device_id"], "sensor-1")
        self.assertEqual(shadow["version"], 0)
        self.assertEqual(shadow["desired"], {})
        self.assertEqual(shadow["reported"], {})
        self.assertEqual(shadow["delta"], {})
        self.assertIsNone(shadow["updated_at"])

    def test_get_unknown_device_shadow_is_not_found(self) -> None:
        status, payload = self.call("GET", "/v1/devices/ghost/shadow")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "device_not_found")

    # ------------------------------------------------------------------
    # desired 写入
    # ------------------------------------------------------------------

    def test_put_desired_returns_full_snapshot(self) -> None:
        status, shadow = self.call(
            "PUT", self.shadow_path("/desired"), {"state": {"power": "on", "n": {"v": 1}}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["desired"], {"power": "on", "n": {"v": 1}})
        self.assertEqual(shadow["reported"], {})
        self.assertEqual(shadow["delta"], {"power": "on", "n": {"v": 1}})
        self.assertTrue(shadow["updated_at"].endswith("Z"))

        # 内容相同再次写入，version 仍递增。
        status, again = self.call(
            "PUT", self.shadow_path("/desired"), {"state": {"power": "on", "n": {"v": 1}}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(again["version"], 2)

    def test_put_desired_unknown_device_is_not_found(self) -> None:
        status, payload = self.call(
            "PUT", "/v1/devices/ghost/shadow/desired", {"state": {"a": 1}}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "device_not_found")

    def test_put_unknown_route_is_not_found(self) -> None:
        status, payload = self.call(
            "PUT", "/v1/devices/sensor-1/shadow/other", {"state": {}}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    # ------------------------------------------------------------------
    # reported 写入
    # ------------------------------------------------------------------

    def test_post_reported_returns_snapshot_and_computes_delta(self) -> None:
        self.call("PUT", self.shadow_path("/desired"),
                  {"state": {"a": 1, "b": {"c": 2, "d": 3}}})
        _, session = self.connect()
        sid = session["session_id"]

        status, shadow = self.call(
            "POST", f"/v1/device-sessions/{sid}/shadow/reported",
            {"session_token": session["session_token"],
             "state": {"a": 1, "b": {"c": 2}, "extra": "ignored"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 2)
        self.assertEqual(shadow["reported"], {"a": 1, "b": {"c": 2}, "extra": "ignored"})
        self.assertEqual(shadow["delta"], {"b": {"d": 3}})

    def test_reported_session_error_codes_over_http(self) -> None:
        # 未知会话。
        status, payload = self.call(
            "POST", "/v1/device-sessions/ghost/shadow/reported",
            {"session_token": "x", "state": {"a": 1}},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

        # 错误令牌。
        _, session = self.connect()
        sid = session["session_id"]
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/shadow/reported",
            {"session_token": "nope", "state": {"a": 1}},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_session_token")

        # 已关闭会话（重连取代）。
        self.connect()
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{sid}/shadow/reported",
            {"session_token": session["session_token"], "state": {"a": 1}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

    # ------------------------------------------------------------------
    # 乐观版本控制
    # ------------------------------------------------------------------

    def test_expected_version_conflict_over_http(self) -> None:
        self.call("PUT", self.shadow_path("/desired"), {"state": {"a": 1}})
        status, payload = self.call(
            "PUT", self.shadow_path("/desired"),
            {"state": {"a": 2}, "expected_version": 0},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "shadow_version_conflict")

        status, shadow = self.call("GET", self.shadow_path())
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["desired"], {"a": 1})

        # 匹配写入前版本即成功。
        status, shadow = self.call(
            "PUT", self.shadow_path("/desired"),
            {"state": {"a": 2}, "expected_version": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 2)

    # ------------------------------------------------------------------
    # 非法请求
    # ------------------------------------------------------------------

    def test_desired_invalid_requests_over_http(self) -> None:
        # 非 JSON 请求体。
        status, payload = self.call(
            "PUT", self.shadow_path("/desired"), raw_body=b"not json{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        for body in (
            {},                          # 缺少 state
            {"state": {"a": 1}, "x": 1},  # 未知字段
            {"state": [1, 2]},           # state 不是对象
            {"state": "x"},
            {"state": None},
            {"state": {"a": 1}, "expected_version": -1},
            {"state": {"a": 1}, "expected_version": "1"},
            {"state": {"a": 1}, "expected_version": 1.5},
            {"state": {"a": 1}, "expected_version": True},
        ):
            with self.subTest(body=body):
                status, payload = self.call("PUT", self.shadow_path("/desired"), body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        status, shadow = self.call("GET", self.shadow_path())
        self.assertEqual(shadow["version"], 0)
        self.assertIsNone(shadow["updated_at"])

    def test_reported_invalid_requests_over_http(self) -> None:
        _, session = self.connect()
        sid = session["session_id"]
        path = f"/v1/device-sessions/{sid}/shadow/reported"
        token = session["session_token"]

        status, payload = self.call("POST", path, raw_body=b"not json{")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        for body in (
            {},
            {"session_token": token},
            {"state": {"a": 1}},
            {"session_token": token, "state": {"a": 1}, "extra": 1},
            {"session_token": token, "state": {"a": 1}, "expected_version": -1},
            {"session_token": token, "state": {"a": 1}, "expected_version": "0"},
            {"session_token": 1, "state": {"a": 1}},
            {"session_token": token, "state": 3},
        ):
            with self.subTest(body=body):
                status, payload = self.call("POST", path, body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        status, shadow = self.call("GET", self.shadow_path())
        self.assertEqual(shadow["version"], 0)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def test_revoked_device_shadow_readable_and_desired_writable(self) -> None:
        self.call("PUT", self.shadow_path("/desired"), {"state": {"a": 1}})
        _, session = self.connect()
        status, _ = self.call("POST", "/v1/devices/sensor-1/revoke")
        self.assertEqual(status, 200)

        status, shadow = self.call("GET", self.shadow_path())
        self.assertEqual(status, 200)
        self.assertEqual(shadow["desired"], {"a": 1})
        self.assertEqual(shadow["version"], 1)

        # 已吊销设备仍可写 desired。
        status, shadow = self.call(
            "PUT", self.shadow_path("/desired"), {"state": {"a": 2}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(shadow["version"], 2)

        # 吊销已关闭其会话，reported 无法再更新。
        status, payload = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/shadow/reported",
            {"session_token": session["session_token"], "state": {"a": 2}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "session_not_online")

        # 已吊销设备也无法新建会话来上报。
        status, payload = self.connect()
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")


if __name__ == "__main__":
    unittest.main()
