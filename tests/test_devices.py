import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from devicefabric.server import Handler
from devicefabric.service import Service, ServiceError


def request_body(**fields: object) -> dict:
    return fields


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_register_returns_device_with_one_time_credential(self) -> None:
        view = self.service.register_device({"device_id": "sensor-01", "display_name": "温度传感器"})
        self.assertEqual(view["device_id"], "sensor-01")
        self.assertEqual(view["display_name"], "温度传感器")
        self.assertTrue(view["active"])
        self.assertEqual(view["credential_version"], 1)
        self.assertIsInstance(view["credential"], str)
        self.assertTrue(view["credential"])
        # RFC 3339 UTC timestamp ending in Z.
        self.assertTrue(view["created_at"].endswith("Z"))

    def test_duplicate_registration_conflicts_and_keeps_record(self) -> None:
        first = self.service.register_device({"device_id": "d1", "display_name": "one"})
        with self.assertRaises(ServiceError) as ctx:
            self.service.register_device({"device_id": "d1", "display_name": "two"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "device_already_exists")
        fetched = self.service.get_device("d1")
        self.assertEqual(fetched["display_name"], "one")
        self.assertNotIn("credential", fetched)
        self.assertTrue(first["credential"])

    def test_get_device_hides_credential(self) -> None:
        self.service.register_device({"device_id": "d1", "display_name": "one"})
        view = self.service.get_device("d1")
        self.assertNotIn("credential", view)
        self.assertEqual(set(view), {"device_id", "display_name", "active", "created_at", "credential_version"})

    def test_get_missing_device_is_404(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_device("ghost")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "device_not_found")

    def test_credentials_are_unique(self) -> None:
        seen = set()
        for i in range(32):
            view = self.service.register_device({"device_id": f"d{i}", "display_name": str(i)})
            self.assertNotIn(view["credential"], seen)
            seen.add(view["credential"])


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_invalid(self, payload: object) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.register_device(payload)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_non_object_bodies(self) -> None:
        for payload in (None, [], "x", 1, 1.5, True):
            with self.subTest(payload=payload):
                self.assert_invalid(payload)

    def test_missing_and_unknown_fields(self) -> None:
        self.assert_invalid({})
        self.assert_invalid({"device_id": "d1"})
        self.assert_invalid({"display_name": "n"})
        self.assert_invalid({"device_id": "d1", "display_name": "n", "extra": 1})

    def test_device_id_rules(self) -> None:
        self.assert_invalid({"device_id": "", "display_name": "n"})
        self.assert_invalid({"device_id": "a/b", "display_name": "n"})
        self.assert_invalid({"device_id": "a b", "display_name": "n"})
        self.assert_invalid({"device_id": "x" * 65, "display_name": "n"})
        self.assert_invalid({"device_id": 1, "display_name": "n"})
        self.assert_invalid({"device_id": ["d"], "display_name": "n"})

    def test_display_name_rules(self) -> None:
        self.assert_invalid({"device_id": "d1", "display_name": ""})
        self.assert_invalid({"device_id": "d1", "display_name": "好" * 129})
        self.assert_invalid({"device_id": "d1", "display_name": 7})
        # 128 Unicode characters is accepted.
        view = self.service.register_device({"device_id": "d1", "display_name": "好" * 128})
        self.assertEqual(len(view["display_name"]), 128)

    def test_invalid_request_creates_no_state(self) -> None:
        self.assert_invalid({"device_id": "", "display_name": "n"})
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_device("")
        self.assertEqual(ctx.exception.code, "device_not_found")


class CredentialLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.view = self.service.register_device({"device_id": "dev", "display_name": "n"})
        self.credential = self.view["credential"]

    def test_authenticate_with_current_credential(self) -> None:
        result = self.service.authenticate({"device_id": "dev", "credential": self.credential})
        self.assertEqual(result, {"authenticated": True})

    def test_authenticate_failures_are_uniform_401(self) -> None:
        for payload in (
            {"device_id": "dev", "credential": "wrong"},
            {"device_id": "ghost", "credential": self.credential},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.authenticate(payload)
                self.assertEqual(ctx.exception.status, 401)
                self.assertEqual(ctx.exception.code, "invalid_credential")

    def test_rotate_invalidates_old_credential_and_bumps_version(self) -> None:
        rotated = self.service.rotate_credential("dev")
        self.assertTrue(rotated["credential"])
        self.assertNotEqual(rotated["credential"], self.credential)
        self.assertEqual(rotated["credential_version"], 2)
        self.assertEqual(self.service.get_device("dev")["credential_version"], 2)
        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate({"device_id": "dev", "credential": self.credential})
        self.assertEqual(ctx.exception.code, "invalid_credential")
        self.assertEqual(
            self.service.authenticate({"device_id": "dev", "credential": rotated["credential"]}),
            {"authenticated": True},
        )

    def test_revoke_is_idempotent_and_blocks_auth(self) -> None:
        view = self.service.revoke_device("dev")
        self.assertFalse(view["active"])
        self.assertEqual(view["credential_version"], 1)
        again = self.service.revoke_device("dev")
        self.assertFalse(again["active"])
        self.assertEqual(again["credential_version"], 1)
        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate({"device_id": "dev", "credential": self.credential})
        self.assertEqual(ctx.exception.code, "invalid_credential")

    def test_rotate_revoked_device_conflicts(self) -> None:
        self.service.revoke_device("dev")
        with self.assertRaises(ServiceError) as ctx:
            self.service.rotate_credential("dev")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "device_revoked")

    def test_missing_device_lifecycle_operations_are_404(self) -> None:
        for operation in (
            lambda: self.service.rotate_credential("ghost"),
            lambda: self.service.revoke_device("ghost"),
        ):
            with self.assertRaises(ServiceError) as ctx:
                operation()
            self.assertEqual(ctx.exception.status, 404)
            self.assertEqual(ctx.exception.code, "device_not_found")


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Handler.service = Service()
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def call(self, method: str, path: str, body: object = ...) -> tuple[int, dict]:
        data = None
        headers = {}
        if body is not ...:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_healthz_remains_available(self) -> None:
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_lifecycle_over_http(self) -> None:
        status, created = self.call("POST", "/v1/devices", {"device_id": "http-1", "display_name": "n"})
        self.assertEqual(status, 201)
        self.assertTrue(created["credential"])

        status, fetched = self.call("GET", "/v1/devices/http-1")
        self.assertEqual(status, 200)
        self.assertNotIn("credential", fetched)

        status, body = self.call("POST", "/v1/devices", {"device_id": "http-1", "display_name": "n"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_already_exists")

        status, body = self.call("POST", "/v1/device-auth", {"device_id": "http-1", "credential": created["credential"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])

        status, body = self.call("POST", "/v1/device-auth", {"device_id": "http-1", "credential": "bad"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_credential")

        status, rotated = self.call("POST", "/v1/devices/http-1/credential/rotate", {})
        self.assertEqual(status, 200)
        self.assertEqual(rotated["credential_version"], 2)

        status, revoked = self.call("POST", "/v1/devices/http-1/revoke", {})
        self.assertEqual(status, 200)
        self.assertFalse(revoked["active"])
        status, _ = self.call("POST", "/v1/devices/http-1/revoke", {})
        self.assertEqual(status, 200)

        status, body = self.call("POST", "/v1/devices/http-1/credential/rotate", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_revoked")

    def test_invalid_and_missing_and_unknown_routes(self) -> None:
        status, body = self.call("POST", "/v1/devices", b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call("POST", "/v1/devices", [1, 2])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call("GET", "/v1/devices/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.call("POST", "/v1/devices/ghost/revoke", {})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
