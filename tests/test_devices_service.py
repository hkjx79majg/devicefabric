import re
import unittest

from devicefabric.service import Service, ServiceError

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def register(self, device_id="sensor-01", display_name="一号传感器"):
        return self.service.register_device(
            {"device_id": device_id, "display_name": display_name}
        )

    def test_register_returns_device_with_onetime_credential(self) -> None:
        result = self.register()
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["display_name"], "一号传感器")
        self.assertTrue(result["active"])
        self.assertEqual(result["credential_version"], 1)
        self.assertIsInstance(result["credential"], str)
        self.assertTrue(result["credential"])
        self.assertTrue(RFC3339_RE.match(result["created_at"]))

    def test_credential_not_visible_on_get(self) -> None:
        created = self.register()
        fetched = self.service.get_device("sensor-01")
        self.assertNotIn("credential", fetched)
        self.assertEqual(fetched["device_id"], created["device_id"])

    def test_duplicate_registration_conflicts_and_keeps_record(self) -> None:
        first = self.register()
        with self.assertRaises(ServiceError) as ctx:
            self.register(display_name="被篡改的名称")
        self.assertEqual(ctx.exception.code, "device_already_exists")
        self.assertEqual(ctx.exception.status, 409)
        fetched = self.service.get_device("sensor-01")
        self.assertEqual(fetched["display_name"], "一号传感器")
        self.assertEqual(fetched["credential_version"], 1)
        self.assertTrue(self.service.authenticate(
            {"device_id": "sensor-01", "credential": first["credential"]}
        )["authenticated"])

    def test_credentials_are_unique_within_process(self) -> None:
        seen = set()
        for i in range(50):
            result = self.register(device_id=f"dev-{i}", display_name=f"dev{i}")
            self.assertNotIn(result["credential"], seen)
            seen.add(result["credential"])

    def test_authenticate_success_and_failures(self) -> None:
        created = self.register()
        ok = self.service.authenticate(
            {"device_id": "sensor-01", "credential": created["credential"]}
        )
        self.assertEqual(ok, {"authenticated": True})

        for bad_credential in ("wrong", ""):
            with self.subTest(credential=bad_credential):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.authenticate(
                        {"device_id": "sensor-01", "credential": bad_credential}
                    )
                self.assertEqual(ctx.exception.code, "invalid_credential")
                self.assertEqual(ctx.exception.status, 401)

        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate(
                {"device_id": "missing", "credential": created["credential"]}
            )
        self.assertEqual(ctx.exception.code, "invalid_credential")

    def test_rotate_credential_bumps_version_and_retires_old(self) -> None:
        created = self.register()
        rotated = self.service.rotate_credential("sensor-01")
        self.assertEqual(rotated["credential_version"], 2)
        self.assertTrue(rotated["credential"])
        self.assertNotEqual(rotated["credential"], created["credential"])

        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate(
                {"device_id": "sensor-01", "credential": created["credential"]}
            )
        self.assertEqual(ctx.exception.code, "invalid_credential")
        self.assertTrue(self.service.authenticate(
            {"device_id": "sensor-01", "credential": rotated["credential"]}
        )["authenticated"])

        again = self.service.rotate_credential("sensor-01")
        self.assertEqual(again["credential_version"], 3)

    def test_revoke_is_idempotent_and_blocks_auth_and_rotation(self) -> None:
        created = self.register()
        revoked = self.service.revoke_device("sensor-01")
        self.assertFalse(revoked["active"])
        self.assertEqual(revoked["credential_version"], 1)

        revoked_again = self.service.revoke_device("sensor-01")
        self.assertFalse(revoked_again["active"])
        self.assertEqual(revoked_again["credential_version"], 1)

        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate(
                {"device_id": "sensor-01", "credential": created["credential"]}
            )
        self.assertEqual(ctx.exception.code, "invalid_credential")

        with self.assertRaises(ServiceError) as ctx:
            self.service.rotate_credential("sensor-01")
        self.assertEqual(ctx.exception.code, "device_revoked")
        self.assertEqual(ctx.exception.status, 409)

    def test_missing_device_operations_return_not_found(self) -> None:
        for call in (
            lambda: self.service.get_device("ghost"),
            lambda: self.service.rotate_credential("ghost"),
            lambda: self.service.revoke_device("ghost"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ServiceError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "device_not_found")
                self.assertEqual(ctx.exception.status, 404)

    def test_invalid_register_payloads(self) -> None:
        bad_payloads = [
            ["not", "an", "object"],
            {},
            {"device_id": "sensor-01"},
            {"display_name": "no id"},
            {"device_id": "sensor-01", "display_name": "ok", "extra": 1},
            {"device_id": "", "display_name": "ok"},
            {"device_id": "x" * 65, "display_name": "ok"},
            {"device_id": "bad/id", "display_name": "ok"},
            {"device_id": "bad id", "display_name": "ok"},
            {"device_id": "sensors!#@", "display_name": "ok"},
            {"device_id": "sensor-01", "display_name": ""},
            {"device_id": "sensor-01", "display_name": "x" * 129},
            {"device_id": 123, "display_name": "ok"},
            {"device_id": "sensor-01", "display_name": 4},
            {"device_id": "sensor-01", "display_name": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.register_device(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 所有失败注册都不得产生任何状态。
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_device("sensor-01")
        self.assertEqual(ctx.exception.code, "device_not_found")

    def test_unicode_display_name_accepted_up_to_128(self) -> None:
        name = "温度" * 64  # 128 Unicode characters
        result = self.register(device_id="uni-1", display_name=name)
        self.assertEqual(result["display_name"], name)

    def test_authenticate_invalid_payloads(self) -> None:
        for payload in (
            "json-string",
            {},
            {"device_id": "sensor-01"},
            {"credential": "x"},
            {"device_id": "sensor-01", "credential": "x", "extra": 1},
            {"device_id": "sensor-01", "credential": 1},
            {"device_id": 5, "credential": "x"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.authenticate(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    def test_authenticate_unknown_or_malformed_device_id_is_unauthorized(self) -> None:
        for device_id in ("missing-device", "bad/id", "bad id", "x" * 65):
            with self.subTest(device_id=device_id):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.authenticate({"device_id": device_id, "credential": "x"})
                self.assertEqual(ctx.exception.code, "invalid_credential")
                self.assertEqual(ctx.exception.status, 401)

    def test_authenticate_with_non_ascii_credential_is_unauthorized(self) -> None:
        self.register()
        # compare_digest 不接受非 ASCII str；必须安全降级为 401 而非 500。
        with self.assertRaises(ServiceError) as ctx:
            self.service.authenticate(
                {"device_id": "sensor-01", "credential": "é凭据🔑"}
            )
        self.assertEqual(ctx.exception.code, "invalid_credential")
        self.assertEqual(ctx.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
