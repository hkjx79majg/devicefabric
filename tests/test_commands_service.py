import re
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class CommandServiceTest(unittest.TestCase):
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

    def create_command(self, name="reboot", payload=None, ttl=60, device_id="sensor-01"):
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

    def ack(self, session, command_id, status="succeeded", result=None):
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

    # --------------------------------------------------------------
    # 创建
    # --------------------------------------------------------------

    def test_create_command_returns_queued_snapshot(self) -> None:
        result = self.create_command(payload={"delay": 3}, ttl=30)
        self.assertTrue(result["command_id"])
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["command_name"], "reboot")
        self.assertEqual(result["payload"], {"delay": 3})
        self.assertEqual(result["ttl_seconds"], 30)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["delivery_count"], 0)
        self.assertIsNone(result["completed_at"])
        self.assertIsNone(result["result"])
        for field in ("created_at", "expires_at"):
            self.assertTrue(RFC3339_RE.match(result[field]), field)

    def test_command_ids_are_unique(self) -> None:
        ids = {self.create_command(name=f"cmd-{i}")["command_id"] for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_create_command_accepts_any_json_payload(self) -> None:
        for payload in (None, True, 1, 1.5, "text", [1, "a"], {"k": [1, {"v": None}]}):
            result = self.create_command(payload=payload)
            self.assertEqual(result["payload"], payload)

    def test_create_command_validation_errors(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"command_name": "reboot", "payload": None},
            {"command_name": "reboot", "ttl_seconds": 30},
            {"payload": None, "ttl_seconds": 30},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 30, "extra": 1},
            {"command_name": "", "payload": None, "ttl_seconds": 30},
            {"command_name": "x" * 65, "payload": None, "ttl_seconds": 30},
            {"command_name": "bad name", "payload": None, "ttl_seconds": 30},
            {"command_name": "bad/name", "payload": None, "ttl_seconds": 30},
            {"command_name": 1, "payload": None, "ttl_seconds": 30},
            {"command_name": True, "payload": None, "ttl_seconds": 30},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 4},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 86401},
            {"command_name": "reboot", "payload": None, "ttl_seconds": "30"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 30.5},
            {"command_name": "reboot", "payload": None, "ttl_seconds": True},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ServiceError) as ctx:
                self.service.create_command("sensor-01", payload)
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._commands, {})

    def test_create_command_unknown_device(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.create_command("no-such-device", {
                "command_name": "reboot", "payload": None, "ttl_seconds": 30,
            })
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_create_command_revoked_device(self) -> None:
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.create_command()
        self.assertEqual(ctx.exception.code, "device_revoked")
        self.assertEqual(ctx.exception.status, 409)

    # --------------------------------------------------------------
    # 查询快照
    # --------------------------------------------------------------

    def test_get_command_returns_full_snapshot(self) -> None:
        created = self.create_command(payload={"a": 1})
        fetched = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(fetched, created)

    def test_get_command_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("no-such-device", "whatever")
        self.assertEqual(ctx.exception.code, "device_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("sensor-01", "no-such-command")
        self.assertEqual(ctx.exception.code, "command_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_get_command_of_other_device_is_not_found(self) -> None:
        created = self.create_command()
        self.service.register_device({"device_id": "sensor-02", "display_name": "二号"})
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command("sensor-02", created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_found")

    # --------------------------------------------------------------
    # 领取
    # --------------------------------------------------------------

    def test_poll_empty_returns_empty_list(self) -> None:
        session = self.create_session()
        self.assertEqual(self.poll(session), {"commands": []})

    def test_poll_claims_in_creation_order(self) -> None:
        first = self.create_command(name="cmd-1")
        second = self.create_command(name="cmd-2")
        session = self.create_session()
        result = self.poll(session)
        self.assertEqual(
            [c["command_id"] for c in result["commands"]],
            [first["command_id"], second["command_id"]],
        )
        for entry in result["commands"]:
            self.assertFalse(entry["dup"])
            self.assertEqual(entry["delivery_count"], 1)
        snapshot = self.service.get_command("sensor-01", first["command_id"])
        self.assertEqual(snapshot["status"], "delivered")
        self.assertEqual(snapshot["delivery_count"], 1)

    def test_poll_respects_max_commands(self) -> None:
        for i in range(3):
            self.create_command(name=f"cmd-{i}")
        session = self.create_session()
        result = self.poll(session, max_commands=2)
        self.assertEqual(len(result["commands"]), 2)

    def test_repoll_before_ack_returns_dup_without_counting(self) -> None:
        created = self.create_command()
        session = self.create_session()
        first = self.poll(session)
        self.assertFalse(first["commands"][0]["dup"])
        again = self.poll(session)
        self.assertEqual(len(again["commands"]), 1)
        entry = again["commands"][0]
        self.assertEqual(entry["command_id"], created["command_id"])
        self.assertTrue(entry["dup"])
        self.assertEqual(entry["delivery_count"], 1)
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["delivery_count"], 1)

    def test_delivered_command_held_by_online_session_is_not_double_claimed(self) -> None:
        self.create_command()
        session_a = self.create_session(client_id="cli-a")
        session_b = self.create_session(client_id="cli-b")
        self.assertEqual(len(self.poll(session_a)["commands"]), 1)
        self.assertEqual(self.poll(session_b), {"commands": []})

    def test_timed_out_claim_can_be_taken_over(self) -> None:
        created = self.create_command()
        session_a = self.create_session(client_id="cli-a")
        self.poll(session_a)
        self.force_expire_session(session_a["session_id"])
        session_b = self.create_session(client_id="cli-b")
        result = self.poll(session_b)
        self.assertEqual(len(result["commands"]), 1)
        entry = result["commands"][0]
        self.assertEqual(entry["command_id"], created["command_id"])
        self.assertTrue(entry["dup"])
        self.assertEqual(entry["delivery_count"], 2)
        # 旧会话已过期，确认按会话保活语义拒绝。
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_a, created["command_id"])
        self.assertEqual(ctx.exception.code, "session_not_online")
        # 新领取会话可以确认。
        acked = self.ack(session_b, created["command_id"], result={"ok": True})
        self.assertEqual(acked["status"], "succeeded")

    def test_replaced_claim_can_be_taken_over(self) -> None:
        created = self.create_command()
        session_a = self.create_session(client_id="cli-a")
        self.poll(session_a)
        # 同 client_id 重连取代旧会话。
        session_b = self.create_session(client_id="cli-a")
        result = self.poll(session_b)
        self.assertEqual(len(result["commands"]), 1)
        self.assertTrue(result["commands"][0]["dup"])
        self.assertEqual(result["commands"][0]["delivery_count"], 2)

    def test_commands_are_scoped_to_their_device(self) -> None:
        self.create_command()
        self.service.register_device({"device_id": "sensor-02", "display_name": "二号"})
        other = self.service.create_session({
            "device_id": "sensor-02",
            "credential": self.service._devices["sensor-02"]["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        self.assertEqual(self.poll(other), {"commands": []})

    def test_poll_validation_errors(self) -> None:
        session = self.create_session()
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": session["session_token"]},
            {"max_commands": 1},
            {"session_token": session["session_token"], "max_commands": 0},
            {"session_token": session["session_token"], "max_commands": 101},
            {"session_token": session["session_token"], "max_commands": "1"},
            {"session_token": session["session_token"], "max_commands": True},
            {"session_token": session["session_token"], "max_commands": 1, "extra": 1},
            {"session_token": 1, "max_commands": 1},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ServiceError) as ctx:
                self.service.poll_commands(session["session_id"], payload)
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)

    def test_poll_session_auth_semantics(self) -> None:
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_commands("no-such-session", {
                "session_token": session["session_token"], "max_commands": 1,
            })
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_commands(session["session_id"], {
                "session_token": "wrong-token", "max_commands": 1,
            })
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)
        self.force_expire_session(session["session_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.poll(session)
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    # --------------------------------------------------------------
    # 确认
    # --------------------------------------------------------------

    def test_ack_success_and_idempotent_replay(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        acked = self.ack(session, created["command_id"], result={"code": 0})
        self.assertEqual(acked["status"], "succeeded")
        self.assertEqual(acked["result"], {"code": 0})
        self.assertTrue(RFC3339_RE.match(acked["completed_at"]))
        # 相同确认幂等。
        replay = self.ack(session, created["command_id"], result={"code": 0})
        self.assertEqual(replay, acked)

    def test_ack_failed_status(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        acked = self.ack(session, created["command_id"], status="failed", result="boom")
        self.assertEqual(acked["status"], "failed")
        self.assertEqual(acked["result"], "boom")

    def test_ack_conflict_after_completion(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        self.ack(session, created["command_id"], result=None)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"], status="failed", result=None)
        self.assertEqual(ctx.exception.code, "command_already_completed")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"], result={"different": True})
        self.assertEqual(ctx.exception.code, "command_already_completed")

    def test_ack_queued_command_is_not_delivered(self) -> None:
        created = self.create_command()
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_delivered")
        self.assertEqual(ctx.exception.status, 409)

    def test_ack_only_accepts_current_claiming_session(self) -> None:
        created = self.create_command()
        session_a = self.create_session(client_id="cli-a")
        session_b = self.create_session(client_id="cli-b")
        self.poll(session_a)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_b, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_delivered")
        self.assertEqual(ctx.exception.status, 409)

    def test_ack_unknown_or_foreign_command(self) -> None:
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, "no-such-command")
        self.assertEqual(ctx.exception.code, "command_not_found")
        self.assertEqual(ctx.exception.status, 404)
        # 属于其他设备的命令按不存在处理。
        created = self.create_command()
        self.service.register_device({"device_id": "sensor-02", "display_name": "二号"})
        other = self.service.create_session({
            "device_id": "sensor-02",
            "credential": self.service._devices["sensor-02"]["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        with self.assertRaises(ServiceError) as ctx:
            self.ack(other, created["command_id"])
        self.assertEqual(ctx.exception.code, "command_not_found")

    def test_ack_validation_errors(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token, "status": "succeeded"},
            {"session_token": token, "result": None},
            {"status": "succeeded", "result": None},
            {"session_token": token, "status": "done", "result": None},
            {"session_token": token, "status": 1, "result": None},
            {"session_token": token, "status": True, "result": None},
            {"session_token": token, "status": "succeeded", "result": None, "extra": 1},
            {"session_token": 1, "status": "succeeded", "result": None},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ServiceError) as ctx:
                self.service.ack_command(
                    session["session_id"], created["command_id"], payload
                )
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)
        # 校验失败不产生状态变化。
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["status"], "delivered")

    def test_ack_session_auth_semantics(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_command("no-such-session", created["command_id"], {
                "session_token": session["session_token"],
                "status": "succeeded",
                "result": None,
            })
        self.assertEqual(ctx.exception.code, "session_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_command(session["session_id"], created["command_id"], {
                "session_token": "wrong-token",
                "status": "succeeded",
                "result": None,
            })
        self.assertEqual(ctx.exception.code, "invalid_session_token")

    # --------------------------------------------------------------
    # 有效期、吊销与凭据轮换
    # --------------------------------------------------------------

    def test_expired_command_is_not_claimable_and_ack_fails(self) -> None:
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

    def test_revoke_cancels_open_commands_but_keeps_terminal(self) -> None:
        queued = self.create_command(name="cmd-1")
        delivered = self.create_command(name="cmd-2")
        done = self.create_command(name="cmd-3")
        session = self.create_session()
        self.poll(session)
        self.ack(session, done["command_id"])
        self.service.revoke_device("sensor-01")
        self.assertEqual(
            self.service.get_command("sensor-01", queued["command_id"])["status"],
            "cancelled",
        )
        self.assertEqual(
            self.service.get_command("sensor-01", delivered["command_id"])["status"],
            "cancelled",
        )
        self.assertEqual(
            self.service.get_command("sensor-01", done["command_id"])["status"],
            "succeeded",
        )

    def test_credential_rotation_does_not_affect_commands(self) -> None:
        created = self.create_command()
        rotated = self.service.rotate_credential("sensor-01")
        snapshot = self.service.get_command("sensor-01", created["command_id"])
        self.assertEqual(snapshot["status"], "queued")
        session = self.create_session(credential=rotated["credential"])
        result = self.poll(session)
        self.assertEqual(len(result["commands"]), 1)

    def test_completed_command_is_not_claimed_again(self) -> None:
        created = self.create_command()
        session = self.create_session()
        self.poll(session)
        self.ack(session, created["command_id"])
        self.assertEqual(self.poll(session), {"commands": []})
