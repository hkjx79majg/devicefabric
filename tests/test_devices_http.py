import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的 Service，保证进程内状态互不影响。
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

    def test_healthz_and_unknown_route_unchanged(self) -> None:
        status, payload = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

        status, payload = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_register_get_auth_lifecycle(self) -> None:
        status, created = self.call("POST", "/v1/devices",
                                    {"device_id": "sensor-1", "display_name": "传感器"})
        self.assertEqual(status, 201)
        self.assertEqual(created["device_id"], "sensor-1")
        self.assertEqual(created["credential_version"], 1)
        self.assertTrue(created["active"])
        self.assertTrue(created["credential"])
        credential = created["credential"]

        status, fetched = self.call("GET", "/v1/devices/sensor-1")
        self.assertEqual(status, 200)
        self.assertNotIn("credential", fetched)

        status, payload = self.call("POST", "/v1/devices",
                                    {"device_id": "sensor-1", "display_name": "另一个"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "device_already_exists")

        status, payload = self.call("POST", "/v1/device-auth",
                                    {"device_id": "sensor-1", "credential": credential})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"authenticated": True})

        status, payload = self.call("POST", "/v1/device-auth",
                                    {"device_id": "sensor-1", "credential": "nope"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

    def test_rotate_and_revoke_over_http(self) -> None:
        _, created = self.call("POST", "/v1/devices",
                               {"device_id": "dev-x", "display_name": "x"})
        old = created["credential"]

        status, rotated = self.call("POST", "/v1/devices/dev-x/credential/rotate")
        self.assertEqual(status, 200)
        self.assertEqual(rotated["credential_version"], 2)
        self.assertTrue(rotated["credential"])
        self.assertNotEqual(rotated["credential"], old)

        status, payload = self.call("POST", "/v1/device-auth",
                                    {"device_id": "dev-x", "credential": old})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_credential")

        status, revoked = self.call("POST", "/v1/devices/dev-x/revoke")
        self.assertEqual(status, 200)
        self.assertFalse(revoked["active"])
        status, revoked_again = self.call("POST", "/v1/devices/dev-x/revoke")
        self.assertEqual(status, 200)
        self.assertEqual(revoked_again["credential_version"], 2)

        status, payload = self.call("POST", "/v1/devices/dev-x/credential/rotate")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "device_revoked")

    def test_not_found_routes_for_unknown_device(self) -> None:
        for method, path in (
            ("GET", "/v1/devices/ghost"),
            ("POST", "/v1/devices/ghost/credential/rotate"),
            ("POST", "/v1/devices/ghost/revoke"),
        ):
            with self.subTest(path=path):
                status, payload = self.call(method, path, body={} if method == "POST" else None)
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"]["code"], "device_not_found")

    def test_invalid_requests(self) -> None:
        # 非 JSON 请求体。
        status, payload = self.call("POST", "/v1/devices", raw_body=b"not json{")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 缺少必填字段。
        status, payload = self.call("POST", "/v1/devices", {"device_id": "a"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 未知字段。
        status, payload = self.call("POST", "/v1/devices",
                                    {"device_id": "a", "display_name": "n", "x": 1})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 字段越界。
        status, payload = self.call("POST", "/v1/devices",
                                    {"device_id": "bad id", "display_name": "n"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        # 失败后状态未产生。
        status, payload = self.call("GET", "/v1/devices/a")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "device_not_found")


if __name__ == "__main__":
    unittest.main()
