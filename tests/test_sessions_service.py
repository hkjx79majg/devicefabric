import re
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class SessionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )

    def create_session(self, client_id="cli-1", keepalive=30, device_id="sensor-01",
                       credential=None):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def force_expire(self, session_id):
        session = self.service._sessions[session_id]
        session["expires_at"] = _utc_now() - timedelta(seconds=1)

    def test_create_session_returns_full_view_with_onetime_token(self) -> None:
        result = self.create_session()
        self.assertTrue(result["session_id"])
        self.assertTrue(result["session_token"])
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["client_id"], "cli-1")
        self.assertTrue(result["online"])
        self.assertEqual(result["state"], "online")
        self.assertIsNone(result["reason"])
        for field in ("connected_at", "last_seen_at", "expires_at"):
            self.assertTrue(RFC3339_RE.match(result[field]), field)

    def test_session_ids_and_tokens_are_unique_and_unpredictable(self) -> None:
        ids, tokens = set(), set()
        for i in range(50):
            result = self.create_session(client_id=f"cli-{i}")
            self.assertNotIn(result["session_id"], ids)
            self.assertNotIn(result["session_token"], tokens)
            ids.add(result["session_id"])
            tokens.add(result["session_token"])

    def test_get_session_returns_snapshot_without_token(self) -> None:
        created = self.create_session()
        fetched = self.service.get_session(created["session_id"])
        self.assertNotIn("session_token", fetched)
        self.assertEqual(fetched["session_id"], created["session_id"])
        self.assertTrue(fetched["online"])

    def test_get_unknown_session_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_session("no-such-session")
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_reconnect_replaces_previous_session(self) -> None:
        first = self.create_session()
        second = self.create_session()
        self.assertNotEqual(first["session_id"], second["session_id"])
        old = self.service.get_session(first["session_id"])
        self.assertFalse(old["online"])
        self.assertEqual(old["state"], "closed")
        self.assertEqual(old["reason"], "replaced")
        self.assertTrue(self.service.get_session(second["session_id"])["online"])

    def test_reconnect_with_other_client_id_keeps_existing_session(self) -> None:
        first = self.create_session(client_id="cli-a")
        self.create_session(client_id="cli-b")
        self.assertTrue(self.service.get_session(first["session_id"])["online"])

    def test_heartbeat_refreshes_last_seen_and_expires(self) -> None:
        created = self.create_session(keepalive=5)
        session = self.service._sessions[created["session_id"]]
        # 模拟会话已建立一段时间，使刷新效果可观察。
        session["last_seen_at"] = session["last_seen_at"] - timedelta(seconds=3)
        session["expires_at"] = session["expires_at"] - timedelta(seconds=3)
        before = self.service.get_session(created["session_id"])

        refreshed = self.service.heartbeat_session(
            created["session_id"], {"session_token": created["session_token"]}
        )
        self.assertNotIn("session_token", refreshed)
        self.assertTrue(refreshed["online"])
        self.assertGreater(refreshed["last_seen_at"], before["last_seen_at"])
        self.assertGreater(refreshed["expires_at"], before["expires_at"])

    def test_heartbeat_unknown_session_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session("ghost", {"session_token": "x"})
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_heartbeat_with_wrong_token_is_unauthorized_and_keeps_times(self) -> None:
        created = self.create_session()
        before = self.service.get_session(created["session_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                created["session_id"], {"session_token": "wrong"}
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)
        after = self.service.get_session(created["session_id"])
        self.assertEqual(after["last_seen_at"], before["last_seen_at"])
        self.assertEqual(after["expires_at"], before["expires_at"])

    def test_expired_session_transitions_on_read_and_cannot_recover(self) -> None:
        created = self.create_session(keepalive=5)
        self.force_expire(created["session_id"])

        snapshot = self.service.get_session(created["session_id"])
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["state"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")

        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                created["session_id"], {"session_token": created["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    def test_expired_session_transitions_on_heartbeat(self) -> None:
        created = self.create_session(keepalive=5)
        self.force_expire(created["session_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                created["session_id"], {"session_token": created["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        snapshot = self.service.get_session(created["session_id"])
        self.assertEqual(snapshot["state"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")

    def test_heartbeat_to_closed_session_conflicts(self) -> None:
        first = self.create_session()
        self.create_session()  # 取代第一个会话
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                first["session_id"], {"session_token": first["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    def test_revoke_closes_online_sessions_and_stays_idempotent(self) -> None:
        created = self.create_session()
        revoked = self.service.revoke_device("sensor-01")
        self.assertFalse(revoked["active"])
        self.assertEqual(revoked["credential_version"], 1)

        snapshot = self.service.get_session(created["session_id"])
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["state"], "closed")
        self.assertEqual(snapshot["reason"], "device_revoked")

        revoked_again = self.service.revoke_device("sensor-01")
        self.assertEqual(revoked_again["credential_version"], 1)
        snapshot = self.service.get_session(created["session_id"])
        self.assertEqual(snapshot["reason"], "device_revoked")

        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                created["session_id"], {"session_token": created["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")

    def test_create_session_credential_failures_are_unauthorized(self) -> None:
        # 设备不存在。
        with self.assertRaises(ServiceError) as ctx:
            self.create_session(device_id="ghost")
        self.assertEqual(ctx.exception.code, "invalid_credential")
        self.assertEqual(ctx.exception.status, 401)

        # 凭据错误。
        with self.assertRaises(ServiceError) as ctx:
            self.create_session(credential="wrong")
        self.assertEqual(ctx.exception.code, "invalid_credential")

        # 凭据已轮换。
        old = self.device["credential"]
        self.service.rotate_credential("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.create_session(credential=old)
        self.assertEqual(ctx.exception.code, "invalid_credential")

        # 设备已吊销。
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.create_session()
        self.assertEqual(ctx.exception.code, "invalid_credential")

    def test_create_session_invalid_payloads(self) -> None:
        credential = self.device["credential"]
        bad_payloads = [
            "not-an-object",
            {},
            {"credential": credential, "client_id": "c", "keepalive_seconds": 5},
            {"device_id": "sensor-01", "client_id": "c", "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c"},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": 5, "extra": 1},
            {"device_id": 1, "credential": credential, "client_id": "c",
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": 1, "client_id": "c",
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": "bad id",
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": "",
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": "x" * 65,
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": 5,
             "keepalive_seconds": 5},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": 4},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": 3601},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": "5"},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": 5.0},
            {"device_id": "sensor-01", "credential": credential, "client_id": "c",
             "keepalive_seconds": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_session(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 所有失败请求都不得产生任何会话。
        self.assertEqual(self.service._sessions, {})

    def test_keepalive_boundaries_accepted(self) -> None:
        for keepalive in (5, 3600):
            with self.subTest(keepalive=keepalive):
                result = self.create_session(client_id=f"cli-{keepalive}",
                                             keepalive=keepalive)
                self.assertTrue(result["online"])

    def test_heartbeat_invalid_payloads(self) -> None:
        created = self.create_session()
        before = self.service.get_session(created["session_id"])
        for payload in (
            "not-an-object",
            {},
            {"session_token": created["session_token"], "extra": 1},
            {"session_token": 123},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.heartbeat_session(created["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        after = self.service.get_session(created["session_id"])
        self.assertEqual(after["last_seen_at"], before["last_seen_at"])
        self.assertEqual(after["expires_at"], before["expires_at"])

    def test_error_responses_never_leak_session_token(self) -> None:
        created = self.create_session()
        self.service.revoke_device("sensor-01")
        try:
            self.service.heartbeat_session(
                created["session_id"], {"session_token": created["session_token"]}
            )
        except ServiceError as exc:
            self.assertNotIn(created["session_token"], exc.message)
        snapshot = self.service.get_session(created["session_id"])
        self.assertNotIn("session_token", snapshot)


if __name__ == "__main__":
    unittest.main()
