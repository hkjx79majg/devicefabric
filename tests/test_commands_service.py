import re
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")

_DEFAULT = object()


class CommandServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.credentials = {"sensor-01": self.device["credential"]}

    def create_session(self, client_id="cli-1", keepalive=30, device_id="sensor-01",
                       credential=None):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential if credential is not None
            else self.credentials[device_id],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def register_device(self, device_id):
        device = self.service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )
        self.credentials[device_id] = device["credential"]
        return device

    def create_command(self, name="reboot", payload=_DEFAULT, ttl=60,
                       device_id="sensor-01"):
        if payload is _DEFAULT:
            payload = {"delay": 3}
        return self.service.create_command(device_id, {
            "command_name": name,
            "payload": payload,
            "ttl_seconds": ttl,
        })

    def poll(self, session, max_commands=10):
        return self.service.poll_commands(session["session_id"], {
            "session_token": session["session_token"],
            "max_commands": max_commands,
        })

    def ack(self, session, command_id, status="succeeded", result=_DEFAULT):
        if result is _DEFAULT:
            result = {"exit_code": 0}
        return self.service.ack_command(session["session_id"], command_id, {
            "session_token": session["session_token"],
            "status": status,
            "result": result,
        })

    def force_expire_command(self, command_id):
        command = self.service._commands[command_id]
        command["expires_at"] = _utc_now() - timedelta(seconds=1)

    def force_expire_session(self, session_id):
        session = self.service._sessions[session_id]
        session["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # 下发
    # ------------------------------------------------------------------

    def test_create_command_returns_queued_snapshot(self) -> None:
        result = self.create_command()
        self.assertTrue(result["command_id"])
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["command_name"], "reboot")
        self.assertEqual(result["payload"], {"delay": 3})
        self.assertEqual(result["ttl_seconds"], 60)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["delivery_count"], 0)
        self.assertIsNone(result["completed_at"])
        self.assertIsNone(result["result"])
        for field in ("created_at", "expires_at"):
            self.assertTrue(RFC3339_RE.match(result[field]), field)

    def test_command_ids_are_unique_and_unpredictable(self) -> None:
        ids = set()
        for i in range(50):
            result = self.create_command(name=f"cmd-{i}")
            self.assertNotIn(result["command_id"], ids)
            ids.add(result["command_id"])

    def test_create_command_accepts_any_json_payload(self) -> None:
        for payload in (None, True, 3, 1.5, "text", [1, 2], {"a": {"b": [None]}}):
            with self.subTest(payload=payload):
                result = self.create_command(payload=payload)
                self.assertEqual(result["payload"], payload)

    def test_create_command_ttl_boundaries_accepted(self) -> None:
        for ttl in (5, 86400):
            with self.subTest(ttl=ttl):
                result = self.create_command(ttl=ttl)
                self.assertEqual(result["ttl_seconds"], ttl)

    def test_create_command_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.create_command(device_id="ghost")
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_create_command_revoked_device_conflicts(self) -> None:
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.create_command()
        self.assertEqual(ctx.exception.code, "device_revoked")
        self.assertEqual(ctx.exception.status, 409)

    def test_create_command_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"payload": None, "ttl_seconds": 60},
            {"command_name": "reboot", "ttl_seconds": 60},
            {"command_name": "reboot", "payload": None},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "extra": 1},
            {"command_name": "", "payload": None, "ttl_seconds": 60},
            {"command_name": "bad name", "payload": None, "ttl_seconds": 60},
            {"command_name": "x" * 65, "payload": None, "ttl_seconds": 60},
            {"command_name": 5, "payload": None, "ttl_seconds": 60},
            {"command_name": True, "payload": None, "ttl_seconds": 60},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 4},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 86401},
            {"command_name": "reboot", "payload": None, "ttl_seconds": "60"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60.0},
            {"command_name": "reboot", "payload": None, "ttl_seconds": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_command("sensor-01", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 所有失败请求都不得产生任何命令。
        self.assertEqual(self.service._commands, {})

    # ------------------------------------------------------------------
    # 查询快照
    # ------------------------------------------------------------------

    def test_get_command_returns_full_snapshot(self) -> None:
        created = self.create_command()
        fetched = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(fetched, created)

    def test_get_command_unknowns_are_not_found(self) -> None:
        created = self.create_command()
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("ghost", created["command_id"])
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("sensor-01", "no-such-command")
        self.assertEqual(ctx.exception.code, "command_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_get_command_other_device_is_not_found(self) -> None:
        other = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号"}
        )
        self.assertTrue(other["credential"])
        created = self.create_command()
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("sensor-02", created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_found")

    # ------------------------------------------------------------------
    # 领取
    # ------------------------------------------------------------------

    def test_poll_empty_returns_empty_list(self) -> None:
        session = self.create_session()
        self.assertEqual(self.poll(session), {"commands": []})

    def test_poll_claims_in_creation_order(self) -> None:
        first = self.create_command(name="cmd-a")
        second = self.create_command(name="cmd-b")
        session = self.create_session()
        commands = self.poll(session)["commands"]
        self.assertEqual(
            [c["command_id"] for c in commands],
            [first["command_id"], second["command_id"]],
        )
        for item, dup in zip(commands, (False, False)):
            self.assertEqual(item["status"], "delivered")
            self.assertEqual(item["delivery_count"], 1)
            self.assertEqual(item["dup"], dup)

    def test_poll_respects_max_commands(self) -> None:
        first = self.create_command(name="cmd-a")
        second = self.create_command(name="cmd-b")
        session = self.create_session()
        commands = self.poll(session, max_commands=1)["commands"]
        self.assertEqual([c["command_id"] for c in commands], [first["command_id"]])
        # 已领取未确认的优先重投，与未领取的共同受 max_commands 限制。
        rest = self.poll(session, max_commands=1)["commands"]
        self.assertEqual(len(rest), 1)
        claimed = {c["command_id"] for c in self.poll(session)["commands"]}
        self.assertIn(second["command_id"], claimed | {first["command_id"]})

    def test_poll_redelivers_unacked_with_dup_and_same_count(self) -> None:
        created = self.create_command()
        session = self.create_session()
        first = self.poll(session)["commands"][0]
        again = self.poll(session)["commands"][0]
        self.assertEqual(again["command_id"], created["command_id"])
        self.assertFalse(first["dup"])
        self.assertTrue(again["dup"])
        self.assertEqual(again["delivery_count"], 1)

    def test_poll_acked_command_is_not_redelivered(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        self.ack(session, created["command_id"])
        self.assertEqual(self.poll(session), {"commands": []})

    def test_other_online_session_cannot_claim_owned_command(self) -> None:
        self.create_command()
        first = self.create_session(client_id="cli-a")
        second = self.create_session(client_id="cli-b")
        self.assertEqual(len(self.poll(first)["commands"]), 1)
        self.assertEqual(self.poll(second), {"commands": []})

    def test_replaced_owner_session_allows_reclaim_with_increment(self) -> None:
        created = self.create_command()
        first = self.create_session()
        self.poll(first)
        # 同组合重连取代旧会话。
        second = self.create_session()
        commands = self.poll(second)["commands"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["command_id"], created["command_id"])
        self.assertTrue(commands[0]["dup"])
        self.assertEqual(commands[0]["delivery_count"], 2)

    def test_expired_owner_session_allows_reclaim(self) -> None:
        created = self.create_command()
        first = self.create_session(client_id="cli-a")
        self.poll(first)
        self.force_expire_session(first["session_id"])
        second = self.create_session(client_id="cli-b")
        commands = self.poll(second)["commands"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["command_id"], created["command_id"])
        self.assertTrue(commands[0]["dup"])
        self.assertEqual(commands[0]["delivery_count"], 2)

    def test_poll_only_returns_own_device_commands(self) -> None:
        self.register_device("sensor-02")
        self.create_command()
        other_session = self.create_session(device_id="sensor-02")
        self.assertEqual(self.poll(other_session), {"commands": []})

    def test_poll_session_auth_semantics(self) -> None:
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_commands("ghost", {
                "session_token": "x", "max_commands": 1,
            })
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_commands(session["session_id"], {
                "session_token": "wrong", "max_commands": 1,
            })
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)
        self.force_expire_session(session["session_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.poll(session)
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    def test_poll_invalid_payloads(self) -> None:
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"max_commands": 1},
            {"session_token": token, "max_commands": 1, "extra": 1},
            {"session_token": 1, "max_commands": 1},
            {"session_token": token, "max_commands": 0},
            {"session_token": token, "max_commands": 101},
            {"session_token": token, "max_commands": "1"},
            {"session_token": token, "max_commands": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.poll_commands(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    # ------------------------------------------------------------------
    # 确认
    # ------------------------------------------------------------------

    def test_ack_completes_command(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        completed = self.ack(session, created["command_id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"], {"exit_code": 0})
        self.assertTrue(RFC3339_RE.match(completed["completed_at"]))
        self.assertEqual(completed["delivery_count"], 1)

    def test_ack_failed_status_is_stored(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        completed = self.ack(session, created["command_id"],
                             status="failed", result="timeout")
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["result"], "timeout")

    def test_ack_same_content_is_idempotent(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        first = self.ack(session, created["command_id"])
        again = self.ack(session, created["command_id"])
        self.assertEqual(again, first)

    def test_ack_conflicting_content_rejected(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        self.ack(session, created["command_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"], result={"exit_code": 1})
        self.assertEqual(ctx.exception.code, "command_already_completed")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"], status="failed")
        self.assertEqual(ctx.exception.code, "command_already_completed")

    def test_ack_undelivered_command_conflicts(self) -> None:
        created = self.create_command()
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_delivered")
        self.assertEqual(ctx.exception.status, 409)
        # 状态不变，仍可正常领取。
        self.assertEqual(
            self.service.get_command("sensor-01", created["command_id"])["status"],
            "queued",
        )

    def test_ack_only_owner_session_accepted(self) -> None:
        created = self.create_command()
        first = self.create_session(client_id="cli-a")
        second = self.create_session(client_id="cli-b")
        self.poll(first)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(second, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_delivered")
        # 归属不变，原会话仍可确认。
        completed = self.ack(first, created["command_id"])
        self.assertEqual(completed["status"], "succeeded")

    def test_ack_after_ownership_transfer(self) -> None:
        created = self.create_command()
        first = self.create_session()
        self.poll(first)
        second = self.create_session()  # 取代旧会话
        self.poll(second)  # 重新领取
        with self.assertRaises(ServiceError) as ctx:
            self.ack(first, created["command_id"])
        self.assertEqual(ctx.exception.code, "session_not_online")
        completed = self.ack(second, created["command_id"])
        self.assertEqual(completed["status"], "succeeded")

    def test_ack_other_device_command_is_not_found(self) -> None:
        self.register_device("sensor-02")
        created = self.create_command()
        other_session = self.create_session(device_id="sensor-02")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(other_session, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(other_session, "no-such-command")
        self.assertEqual(ctx.exception.code, "command_not_found")

    def test_ack_session_auth_semantics(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_command("ghost", created["command_id"], {
                "session_token": "x", "status": "succeeded", "result": None,
            })
        self.assertEqual(ctx.exception.code, "session_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_command(session["session_id"], created["command_id"], {
                "session_token": "wrong", "status": "succeeded", "result": None,
            })
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)

    def test_ack_invalid_payloads(self) -> None:
        created = self.create_command()
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token, "status": "succeeded"},
            {"session_token": token, "result": None},
            {"status": "succeeded", "result": None},
            {"session_token": token, "status": "succeeded", "result": None,
             "extra": 1},
            {"session_token": 1, "status": "succeeded", "result": None},
            {"session_token": token, "status": "done", "result": None},
            {"session_token": token, "status": 1, "result": None},
            {"session_token": token, "status": True, "result": None},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.ack_command(
                        session["session_id"], created["command_id"], payload
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 失败请求不改变命令状态。
        self.assertEqual(
            self.service.get_command("sensor-01", created["command_id"])["status"],
            "queued",
        )

    # ------------------------------------------------------------------
    # 过期、吊销与凭据轮换
    # ------------------------------------------------------------------

    def test_expired_command_not_claimable_and_ack_rejected(self) -> None:
        created = self.create_command(ttl=5)
        session = self.create_session()
        self.force_expire_command(created["command_id"])
        self.assertEqual(self.poll(session), {"commands": []})
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["status"], "expired")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_expired")
        self.assertEqual(ctx.exception.status, 409)

    def test_delivered_command_expires_before_ack(self) -> None:
        created = self.create_command(ttl=5)
        session = self.create_session()
        self.poll(session)
        self.force_expire_command(created["command_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_expired")
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["status"], "expired")
        self.assertEqual(snapshot["delivery_count"], 1)

    def test_revoke_cancels_non_terminal_commands(self) -> None:
        queued = self.create_command(name="cmd-a")
        session = self.create_session()
        delivered = self.create_command(name="cmd-b")
        self.poll(session, max_commands=1)  # 只领取最早的 queued
        completed = self.create_command(name="cmd-c")
        self.poll(session)
        self.ack(session, completed["command_id"])

        self.service.revoke_device("sensor-01")
        self.assertEqual(
            self.service.get_command("sensor-01", queued["command_id"])["status"],
            "cancelled",
        )
        self.assertEqual(
            self.service.get_command("sensor-01", delivered["command_id"])["status"],
            "cancelled",
        )
        # 已确认命令保持终态不变。
        snapshot = self.service.get_command("sensor-01", completed["command_id"])
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["result"], {"exit_code": 0})

    def test_credential_rotation_keeps_commands(self) -> None:
        created = self.create_command()
        self.service.rotate_credential("sensor-01")
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["status"], "queued")

    def test_commands_cleared_with_process_state(self) -> None:
        # 新 Service 实例模拟进程重启：命令不保留。
        self.create_command()
        fresh = Service()
        fresh.register_device({"device_id": "sensor-01", "display_name": "一号"})
        self.assertEqual(fresh._commands, {})


if __name__ == "__main__":
    unittest.main()
