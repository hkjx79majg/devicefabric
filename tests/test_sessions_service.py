import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from devicefabric import service as service_mod
from devicefabric.service import Service, ServiceError

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


def parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SessionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        created = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.credential = created["credential"]

    def connect(self, *, device_id="sensor-01", credential=None,
                client_id="client-a", keepalive_seconds=30):
        return self.service.create_session(
            {
                "device_id": device_id,
                "credential": self.credential if credential is None else credential,
                "client_id": client_id,
                "keepalive_seconds": keepalive_seconds,
            }
        )

    def test_create_session_returns_online_snapshot_with_onetime_token(self) -> None:
        result = self.connect(keepalive_seconds=60)
        self.assertIsInstance(result["session_id"], str)
        self.assertTrue(result["session_id"])
        self.assertIsInstance(result["session_token"], str)
        self.assertTrue(result["session_token"])
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["client_id"], "client-a")
        self.assertTrue(result["online"])
        self.assertNotIn("status", result)
        for field in ("connected_at", "last_seen_at", "expires_at"):
            self.assertTrue(RFC3339_RE.match(result[field]), field)
        connected_at = parse_rfc3339(result["connected_at"])
        expires_at = parse_rfc3339(result["expires_at"])
        self.assertEqual(expires_at - connected_at, timedelta(seconds=60))
        self.assertEqual(result["connected_at"], result["last_seen_at"])

        # 普通快照绝不含 session_token。
        snapshot = self.service.get_session(result["session_id"])
        self.assertNotIn("session_token", snapshot)
        self.assertEqual(snapshot["session_id"], result["session_id"])
        self.assertTrue(snapshot["online"])

    def test_session_ids_and_tokens_are_unique_and_unpredictable(self) -> None:
        first = self.connect(client_id="c1")
        second = self.connect(client_id="c2")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["session_token"], second["session_token"])
        # 令牌不得等于凭据，也不得出现在快照中。
        self.assertNotEqual(first["session_token"], self.credential)

    def test_keepalive_bounds(self) -> None:
        self.connect(keepalive_seconds=5)
        self.connect(client_id="max", keepalive_seconds=3600)

    def test_create_invalid_payloads(self) -> None:
        base = {
            "device_id": "sensor-01",
            "credential": self.credential,
            "client_id": "client-a",
            "keepalive_seconds": 30,
        }
        bad_payloads = [
            "json-string",
            ["not", "object"],
            {},
            {k: v for k, v in base.items() if k != "device_id"},
            {k: v for k, v in base.items() if k != "credential"},
            {k: v for k, v in base.items() if k != "client_id"},
            {k: v for k, v in base.items() if k != "keepalive_seconds"},
            {**base, "extra": 1},
            {**base, "device_id": 5},
            {**base, "credential": 1},
            {**base, "client_id": 4},
            {**base, "client_id": True},
            {**base, "client_id": "bad/id"},
            {**base, "client_id": "bad id"},
            {**base, "client_id": "x" * 65},
            {**base, "keepalive_seconds": 4},
            {**base, "keepalive_seconds": 3601},
            {**base, "keepalive_seconds": 0},
            {**base, "keepalive_seconds": 30.0},
            {**base, "keepalive_seconds": "30"},
            {**base, "keepalive_seconds": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_session(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    def test_create_with_bad_credential_states_is_unauthorized(self) -> None:
        for payload_kwargs in (
            {"device_id": "ghost", "credential": self.credential},
            {"device_id": "sensor-01", "credential": "wrong"},
            {"device_id": "sensor-01", "credential": ""},
        ):
            with self.subTest(kwargs=payload_kwargs):
                with self.assertRaises(ServiceError) as ctx:
                    self.connect(**payload_kwargs)
                self.assertEqual(ctx.exception.code, "invalid_credential")
                self.assertEqual(ctx.exception.status, 401)

    def test_rotated_credential_blocks_new_sessions(self) -> None:
        rotated = self.service.rotate_credential("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.code, "invalid_credential")
        # 新凭据仍然可以建连。
        result = self.connect(credential=rotated["credential"])
        self.assertTrue(result["online"])

    def test_revoked_device_cannot_create_session(self) -> None:
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.code, "invalid_credential")
        self.assertEqual(ctx.exception.status, 401)

    def test_reconnect_replaces_previous_session(self) -> None:
        first = self.connect()
        second = self.connect()  # 同 device_id + client_id
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertTrue(second["online"])

        old_snapshot = self.service.get_session(first["session_id"])
        self.assertFalse(old_snapshot["online"])
        self.assertEqual(old_snapshot["status"], "closed")
        self.assertEqual(old_snapshot["reason"], "replaced")

        # 旧会话的心跳被拒绝。
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                first["session_id"], {"session_token": first["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    def test_different_clients_keep_distinct_sessions(self) -> None:
        first = self.connect(client_id="c1")
        second = self.connect(client_id="c2")
        self.assertTrue(self.service.get_session(first["session_id"])["online"])
        self.assertTrue(self.service.get_session(second["session_id"])["online"])

    def test_failed_reconnect_keeps_existing_session_online(self) -> None:
        first = self.connect()
        # 凭据错误的重连不得取代既有会话。
        with self.assertRaises(ServiceError) as ctx:
            self.connect(credential="wrong")
        self.assertEqual(ctx.exception.code, "invalid_credential")
        snapshot = self.service.get_session(first["session_id"])
        self.assertTrue(snapshot["online"])
        # 原令牌心跳仍然成功。
        beat = self.service.heartbeat_session(
            first["session_id"], {"session_token": first["session_token"]}
        )
        self.assertTrue(beat["online"])

    def test_heartbeat_refreshes_times(self) -> None:
        result = self.connect(keepalive_seconds=60)
        before_last_seen = result["last_seen_at"]
        before_expires = parse_rfc3339(result["expires_at"])

        beat = self.service.heartbeat_session(
            result["session_id"], {"session_token": result["session_token"]}
        )
        self.assertEqual(beat["status"] if "status" in beat else "online", "online")
        self.assertTrue(beat["online"])
        self.assertGreaterEqual(beat["last_seen_at"], before_last_seen)
        self.assertGreater(parse_rfc3339(beat["expires_at"]), before_expires)
        self.assertEqual(
            parse_rfc3339(beat["expires_at"]) - parse_rfc3339(beat["last_seen_at"]),
            timedelta(seconds=60),
        )
        self.assertNotIn("session_token", beat)

    def test_heartbeat_wrong_token_is_unauthorized_and_does_not_refresh(self) -> None:
        result = self.connect(keepalive_seconds=60)
        before = self.service.get_session(result["session_id"])
        for token in ("wrong", ""):
            with self.subTest(token=token):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.heartbeat_session(
                        result["session_id"], {"session_token": token}
                    )
                self.assertEqual(ctx.exception.code, "invalid_session_token")
                self.assertEqual(ctx.exception.status, 401)
        # 失败请求不刷新任何时间，会话仍在线。
        after = self.service.get_session(result["session_id"])
        self.assertTrue(after["online"])
        self.assertEqual(after["last_seen_at"], before["last_seen_at"])
        self.assertEqual(after["expires_at"], before["expires_at"])

    def test_heartbeat_invalid_payloads(self) -> None:
        result = self.connect()
        for payload in (
            "x",
            {},
            {"session_token": result["session_token"], "extra": 1},
            {"session_token": 1},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.heartbeat_session(result["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    def test_get_and_heartbeat_unknown_session(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_session("nope-not-real")
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                "nope-not-real", {"session_token": "whatever"}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_session_expires_and_cannot_recover(self) -> None:
        result = self.connect(keepalive_seconds=5)
        future = _utc_real() + timedelta(seconds=6)
        with mock.patch.object(service_mod, "_utc_now", return_value=future):
            snapshot = self.service.get_session(result["session_id"])
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["status"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")

        # 正确令牌也无法救活过期会话。
        with self.assertRaises(ServiceError) as ctx:
            self.service.heartbeat_session(
                result["session_id"], {"session_token": result["session_token"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

        snapshot = self.service.get_session(result["session_id"])
        self.assertEqual(snapshot["status"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")

    def test_expired_session_does_not_become_replaced_on_reconnect(self) -> None:
        first = self.connect(keepalive_seconds=5)
        future = _utc_real() + timedelta(seconds=6)
        with mock.patch.object(service_mod, "_utc_now", return_value=future):
            second = self.connect()
        self.assertTrue(second["online"])
        old = self.service.get_session(first["session_id"])
        self.assertEqual(old["status"], "expired")
        self.assertEqual(old["reason"], "keepalive_timeout")

    def test_revoke_closes_online_sessions_immediately(self) -> None:
        first = self.connect(client_id="c1")
        second = self.connect(client_id="c2")
        revoked = self.service.revoke_device("sensor-01")
        self.assertFalse(revoked["active"])

        for created in (first, second):
            snapshot = self.service.get_session(created["session_id"])
            self.assertFalse(snapshot["online"])
            self.assertEqual(snapshot["status"], "closed")
            self.assertEqual(snapshot["reason"], "device_revoked")
            with self.assertRaises(ServiceError) as ctx:
                self.service.heartbeat_session(
                    created["session_id"],
                    {"session_token": created["session_token"]},
                )
            self.assertEqual(ctx.exception.code, "session_not_online")

    def test_repeat_revoke_keeps_existing_close_reason(self) -> None:
        first = self.connect()
        self.service.revoke_device("sensor-01")
        self.service.revoke_device("sensor-01")  # 幂等，不改变会话
        snapshot = self.service.get_session(first["session_id"])
        self.assertEqual(snapshot["status"], "closed")
        self.assertEqual(snapshot["reason"], "device_revoked")

    def test_revoke_leaves_expired_session_expired(self) -> None:
        result = self.connect(keepalive_seconds=5)
        future = _utc_real() + timedelta(seconds=6)
        with mock.patch.object(service_mod, "_utc_now", return_value=future):
            self.service.get_session(result["session_id"])
        self.service.revoke_device("sensor-01")
        snapshot = self.service.get_session(result["session_id"])
        self.assertEqual(snapshot["status"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")


def _utc_real() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    unittest.main()
