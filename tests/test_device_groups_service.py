import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class DeviceGroupServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.credentials = {}
        for device_id in ("sensor-01", "sensor-02", "sensor-03"):
            self.register_device(device_id)

    def register_device(self, device_id):
        device = self.service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )
        self.credentials[device_id] = device["credential"]
        return device

    def create_group(self, group_id="group-a", device_ids=("sensor-01", "sensor-02")):
        return self.service.create_group({
            "group_id": group_id,
            "device_ids": list(device_ids),
        })

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def test_create_group_returns_version_one_and_ordered_members(self) -> None:
        result = self.create_group(device_ids=("sensor-02", "sensor-01"))
        self.assertEqual(result["group_id"], "group-a")
        self.assertEqual(result["device_ids"], ["sensor-02", "sensor-01"])
        self.assertEqual(result["version"], 1)

    def test_create_group_allows_empty_members(self) -> None:
        result = self.create_group(device_ids=())
        self.assertEqual(result["device_ids"], [])
        self.assertEqual(result["version"], 1)

    def test_create_group_duplicate_name_conflicts(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.create_group(device_ids=("sensor-03",))
        self.assertEqual(ctx.exception.code, "group_already_exists")
        self.assertEqual(ctx.exception.status, 409)
        # 原组保持不变。
        group = self.service.get_group("group-a")
        self.assertEqual(group["device_ids"], ["sensor-01", "sensor-02"])
        self.assertEqual(group["version"], 1)

    def test_create_group_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.create_group(device_ids=("sensor-01", "ghost"))
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_group("group-a")
        self.assertEqual(ctx.exception.code, "group_not_found")

    def test_create_group_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"group_id": "group-a"},
            {"device_ids": []},
            {"group_id": "group-a", "device_ids": [], "extra": 1},
            {"group_id": "", "device_ids": []},
            {"group_id": "bad name", "device_ids": []},
            {"group_id": "x" * 65, "device_ids": []},
            {"group_id": 5, "device_ids": []},
            {"group_id": True, "device_ids": []},
            {"group_id": "group-a", "device_ids": "sensor-01"},
            {"group_id": "group-a", "device_ids": ["sensor-01", "sensor-01"]},
            {"group_id": "group-a", "device_ids": ["sensor-01", 5]},
            {"group_id": "group-a", "device_ids": ["bad name"]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_group(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._groups, {})

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def test_get_group_returns_snapshot(self) -> None:
        created = self.create_group()
        fetched = self.service.get_group("group-a")
        self.assertEqual(fetched, created)

    def test_get_group_unknown_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_group("ghost")
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_revoke_device_keeps_group_membership(self) -> None:
        self.create_group()
        self.service.revoke_device("sensor-01")
        group = self.service.get_group("group-a")
        self.assertEqual(group["device_ids"], ["sensor-01", "sensor-02"])
        self.assertEqual(group["version"], 1)

    # ------------------------------------------------------------------
    # 整体替换成员
    # ------------------------------------------------------------------

    def test_replace_members_increments_version(self) -> None:
        self.create_group()
        replaced = self.service.replace_group_members(
            "group-a", {"device_ids": ["sensor-03", "sensor-01"]}
        )
        self.assertEqual(replaced["device_ids"], ["sensor-03", "sensor-01"])
        self.assertEqual(replaced["version"], 2)
        self.assertEqual(self.service.get_group("group-a"), replaced)

    def test_replace_same_members_still_increments_version(self) -> None:
        self.create_group()
        replaced = self.service.replace_group_members(
            "group-a", {"device_ids": ["sensor-01", "sensor-02"]}
        )
        self.assertEqual(replaced["version"], 2)
        self.assertEqual(replaced["device_ids"], ["sensor-01", "sensor-02"])

    def test_replace_members_with_matching_expected_version(self) -> None:
        self.create_group()
        replaced = self.service.replace_group_members("group-a", {
            "device_ids": ["sensor-03"],
            "expected_version": 1,
        })
        self.assertEqual(replaced["version"], 2)

    def test_replace_members_version_conflict_keeps_group(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members("group-a", {
                "device_ids": ["sensor-03"],
                "expected_version": 3,
            })
        self.assertEqual(ctx.exception.code, "group_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        group = self.service.get_group("group-a")
        self.assertEqual(group["device_ids"], ["sensor-01", "sensor-02"])
        self.assertEqual(group["version"], 1)

    def test_replace_members_unknown_group_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members("ghost", {"device_ids": []})
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_replace_members_unknown_device_keeps_group(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members(
                "group-a", {"device_ids": ["sensor-01", "ghost"]}
            )
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)
        group = self.service.get_group("group-a")
        self.assertEqual(group["device_ids"], ["sensor-01", "sensor-02"])
        self.assertEqual(group["version"], 1)

    def test_replace_members_invalid_payloads(self) -> None:
        self.create_group()
        bad_payloads = [
            "not-an-object",
            {},
            {"expected_version": 1},
            {"device_ids": [], "extra": 1},
            {"device_ids": "sensor-01"},
            {"device_ids": ["sensor-01", "sensor-01"]},
            {"device_ids": [True]},
            {"device_ids": [], "expected_version": -1},
            {"device_ids": [], "expected_version": 1.5},
            {"device_ids": [], "expected_version": True},
            {"device_ids": [], "expected_version": "1"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.replace_group_members("group-a", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        group = self.service.get_group("group-a")
        self.assertEqual(group["device_ids"], ["sensor-01", "sensor-02"])
        self.assertEqual(group["version"], 1)


class CommandBatchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.credentials = {}
        for device_id in ("sensor-01", "sensor-02", "sensor-03"):
            self.register_device(device_id)
        self.service.create_group({
            "group_id": "group-a",
            "device_ids": ["sensor-01", "sensor-02"],
        })

    def register_device(self, device_id):
        device = self.service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )
        self.credentials[device_id] = device["credential"]
        return device

    def create_session(self, device_id="sensor-01", client_id="cli-1"):
        return self.service.create_session({
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": client_id,
            "keepalive_seconds": 30,
        })

    def create_batch(self, group_id="group-a", name="reboot", payload=None,
                     ttl=60, request_id="req-1", expected_group_version=None):
        body = {
            "command_name": name,
            "payload": {"delay": 3} if payload is None else payload,
            "ttl_seconds": ttl,
            "request_id": request_id,
        }
        if expected_group_version is not None:
            body["expected_group_version"] = expected_group_version
        return self.service.create_command_batch(group_id, body)

    # ------------------------------------------------------------------
    # 下发
    # ------------------------------------------------------------------

    def test_create_batch_returns_member_ordered_commands(self) -> None:
        result = self.create_batch()
        self.assertTrue(result["batch_id"])
        self.assertEqual(result["group_version"], 1)
        self.assertEqual(
            [item["device_id"] for item in result["commands"]],
            ["sensor-01", "sensor-02"],
        )
        command_ids = [item["command_id"] for item in result["commands"]]
        self.assertEqual(len(set(command_ids)), 2)
        for item, device_id in zip(result["commands"], ("sensor-01", "sensor-02")):
            snapshot = self.service.get_command(device_id, item["command_id"])
            self.assertEqual(snapshot["status"], "queued")
            self.assertEqual(snapshot["command_name"], "reboot")
            self.assertEqual(snapshot["payload"], {"delay": 3})
            self.assertEqual(snapshot["ttl_seconds"], 60)

    def test_batch_ids_are_unique_and_unpredictable(self) -> None:
        ids = set()
        for i in range(20):
            result = self.create_batch(request_id=f"req-{i}")
            self.assertNotIn(result["batch_id"], ids)
            ids.add(result["batch_id"])

    def test_create_batch_empty_group_creates_zero_item_batch(self) -> None:
        self.service.create_group({"group_id": "empty", "device_ids": []})
        result = self.create_batch(group_id="empty")
        self.assertEqual(result["commands"], [])
        fetched = self.service.get_command_batch("empty", result["batch_id"])
        self.assertEqual(fetched["commands"], [])
        self.assertEqual(fetched["counts"], {
            "queued": 0, "delivered": 0, "succeeded": 0,
            "failed": 0, "expired": 0, "cancelled": 0,
        })

    def test_create_batch_with_matching_expected_group_version(self) -> None:
        result = self.create_batch(expected_group_version=1)
        self.assertEqual(result["group_version"], 1)

    def test_create_batch_version_conflict_creates_nothing(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch(expected_group_version=7)
        self.assertEqual(ctx.exception.code, "group_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service._commands, {})
        self.assertEqual(self.service._batches, {})

    def test_create_batch_unknown_group_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch(group_id="ghost")
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_create_batch_with_revoked_member_creates_nothing(self) -> None:
        self.service.revoke_device("sensor-02")
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch()
        self.assertEqual(ctx.exception.code, "group_contains_revoked_device")
        self.assertEqual(ctx.exception.status, 409)
        # 不得创建部分命令。
        self.assertEqual(self.service._commands, {})
        self.assertEqual(self.service._batches, {})

    def test_create_batch_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"payload": None, "ttl_seconds": 60, "request_id": "r"},
            {"command_name": "reboot", "ttl_seconds": 60, "request_id": "r"},
            {"command_name": "reboot", "payload": None, "request_id": "r"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": "r", "extra": 1},
            {"command_name": "bad name", "payload": None, "ttl_seconds": 60,
             "request_id": "r"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 4,
             "request_id": "r"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 86401,
             "request_id": "r"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": "60",
             "request_id": "r"},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": ""},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": "x" * 65},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": 5},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": True},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": "r", "expected_group_version": -1},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": "r", "expected_group_version": 1.5},
            {"command_name": "reboot", "payload": None, "ttl_seconds": 60,
             "request_id": "r", "expected_group_version": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_command_batch("group-a", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._commands, {})
        self.assertEqual(self.service._batches, {})

    # ------------------------------------------------------------------
    # request_id 幂等
    # ------------------------------------------------------------------

    def test_same_request_id_same_content_returns_original_batch(self) -> None:
        first = self.create_batch()
        again = self.create_batch()
        self.assertEqual(again, first)
        # 不创建新命令。
        self.assertEqual(len(self.service._commands), 2)
        self.assertEqual(len(self.service._batches), 1)

    def test_same_request_id_different_content_conflicts(self) -> None:
        self.create_batch()
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch(name="shutdown")
        self.assertEqual(ctx.exception.code, "batch_request_conflict")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch(payload={"delay": 9})
        self.assertEqual(ctx.exception.code, "batch_request_conflict")
        with self.assertRaises(ServiceError) as ctx:
            self.create_batch(ttl=120)
        self.assertEqual(ctx.exception.code, "batch_request_conflict")
        self.assertEqual(len(self.service._batches), 1)

    def test_same_request_id_in_other_group_is_independent(self) -> None:
        self.service.create_group({"group_id": "group-b", "device_ids": ["sensor-03"]})
        first = self.create_batch()
        other = self.create_batch(group_id="group-b")
        self.assertNotEqual(first["batch_id"], other["batch_id"])

    # ------------------------------------------------------------------
    # 批次查询
    # ------------------------------------------------------------------

    def test_get_batch_returns_snapshots_in_member_order(self) -> None:
        created = self.create_batch()
        fetched = self.service.get_command_batch("group-a", created["batch_id"])
        self.assertEqual(fetched["batch_id"], created["batch_id"])
        self.assertEqual(fetched["group_version"], 1)
        self.assertEqual(
            [c["device_id"] for c in fetched["commands"]],
            ["sensor-01", "sensor-02"],
        )
        self.assertEqual(
            [c["command_id"] for c in fetched["commands"]],
            [item["command_id"] for item in created["commands"]],
        )
        self.assertEqual(fetched["counts"], {
            "queued": 2, "delivered": 0, "succeeded": 0,
            "failed": 0, "expired": 0, "cancelled": 0,
        })

    def test_get_batch_unknown_is_not_found(self) -> None:
        created = self.create_batch()
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command_batch("group-a", "no-such-batch")
        self.assertEqual(ctx.exception.code, "batch_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command_batch("ghost", created["batch_id"])
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_get_batch_under_other_group_is_not_found(self) -> None:
        self.service.create_group({"group_id": "group-b", "device_ids": []})
        created = self.create_batch()
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command_batch("group-b", created["batch_id"])
        self.assertEqual(ctx.exception.code, "batch_not_found")

    def test_member_change_does_not_affect_existing_batch(self) -> None:
        created = self.create_batch()
        self.service.replace_group_members(
            "group-a", {"device_ids": ["sensor-03"]}
        )
        fetched = self.service.get_command_batch("group-a", created["batch_id"])
        self.assertEqual(fetched["group_version"], 1)
        self.assertEqual(
            [c["device_id"] for c in fetched["commands"]],
            ["sensor-01", "sensor-02"],
        )
        # 新批次采用新成员与新版本。
        second = self.create_batch(request_id="req-2")
        self.assertEqual(second["group_version"], 2)
        self.assertEqual(
            [item["device_id"] for item in second["commands"]], ["sensor-03"]
        )

    def test_sub_commands_use_existing_poll_and_ack(self) -> None:
        created = self.create_batch()
        command_id = created["commands"][0]["command_id"]
        session = self.create_session()
        polled = self.service.poll_commands(session["session_id"], {
            "session_token": session["session_token"],
            "max_commands": 10,
        })
        self.assertEqual(
            [c["command_id"] for c in polled["commands"]], [command_id]
        )
        self.service.ack_command(session["session_id"], command_id, {
            "session_token": session["session_token"],
            "status": "succeeded",
            "result": {"exit_code": 0},
        })
        fetched = self.service.get_command_batch("group-a", created["batch_id"])
        self.assertEqual(fetched["counts"]["succeeded"], 1)
        self.assertEqual(fetched["counts"]["queued"], 1)
        self.assertEqual(fetched["commands"][0]["status"], "succeeded")

    def test_revoke_device_cancels_non_terminal_sub_commands(self) -> None:
        created = self.create_batch()
        self.service.revoke_device("sensor-01")
        fetched = self.service.get_command_batch("group-a", created["batch_id"])
        self.assertEqual(fetched["counts"]["cancelled"], 1)
        self.assertEqual(fetched["counts"]["queued"], 1)
        self.assertEqual(fetched["commands"][0]["status"], "cancelled")

    def test_expired_sub_commands_are_counted(self) -> None:
        created = self.create_batch(ttl=5)
        for command in self.service._commands.values():
            command["expires_at"] = _utc_now() - timedelta(seconds=1)
        fetched = self.service.get_command_batch("group-a", created["batch_id"])
        self.assertEqual(fetched["counts"]["expired"], 2)


if __name__ == "__main__":
    unittest.main()
