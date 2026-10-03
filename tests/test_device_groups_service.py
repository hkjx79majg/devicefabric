import threading
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class DeviceGroupServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "设备甲"}
        )
        self.service.register_device(
            {"device_id": "dev-b", "display_name": "设备乙"}
        )
        self.service.register_device(
            {"device_id": "dev-c", "display_name": "设备丙"}
        )

    def create_group(self, group_id="grp-1", device_ids=("dev-a", "dev-b")):
        return self.service.create_group(
            {"group_id": group_id, "device_ids": list(device_ids)}
        )

    # ------------------------------------------------------------------
    # 创建与查询
    # ------------------------------------------------------------------

    def test_create_group_returns_version_one_and_ordered_members(self) -> None:
        result = self.create_group(device_ids=("dev-c", "dev-a"))
        self.assertEqual(result["group_id"], "grp-1")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["device_ids"], ["dev-c", "dev-a"])

    def test_create_empty_group(self) -> None:
        result = self.create_group(group_id="grp-empty", device_ids=())
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["device_ids"], [])

    def test_get_group_returns_snapshot(self) -> None:
        created = self.create_group()
        fetched = self.service.get_group("grp-1")
        self.assertEqual(fetched, created)
        # 返回副本，调用方修改不影响进程内状态。
        fetched["device_ids"].append("dev-c")
        self.assertEqual(self.service.get_group("grp-1")["device_ids"],
                         ["dev-a", "dev-b"])

    def test_get_unknown_group_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_group("ghost")
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_create_duplicate_group_conflicts(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.create_group()
        self.assertEqual(ctx.exception.code, "group_already_exists")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service.get_group("grp-1")["version"], 1)

    def test_create_group_with_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.create_group(device_ids=("dev-a", "ghost"))
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)
        self.assertNotIn("grp-1", self.service._groups)

    def test_create_group_with_revoked_device_allowed(self) -> None:
        # 已注册但已吊销的设备仍可成为成员。
        self.service.revoke_device("dev-b")
        result = self.create_group()
        self.assertEqual(result["device_ids"], ["dev-a", "dev-b"])

    def test_create_group_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"device_ids": ["dev-a"]},
            {"group_id": "grp-1"},
            {"group_id": "grp-1", "device_ids": ["dev-a"], "extra": 1},
            {"group_id": "", "device_ids": []},
            {"group_id": "bad group", "device_ids": []},
            {"group_id": "x" * 65, "device_ids": []},
            {"group_id": 1, "device_ids": []},
            {"group_id": True, "device_ids": []},
            {"group_id": "grp-1", "device_ids": "dev-a"},
            {"group_id": "grp-1", "device_ids": [1]},
            {"group_id": "grp-1", "device_ids": [None]},
            {"group_id": "grp-1", "device_ids": ["dev-a", "dev-a"]},
            {"group_id": "grp-1", "device_ids": ["bad id"]},
            {"group_id": "grp-1", "device_ids": ["x" * 65]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_group(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._groups, {})

    # ------------------------------------------------------------------
    # 整体替换成员
    # ------------------------------------------------------------------

    def test_replace_members_overwrites_and_increments(self) -> None:
        self.create_group()
        result = self.service.replace_group_members(
            "grp-1", {"device_ids": ["dev-c", "dev-a"]}
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["device_ids"], ["dev-c", "dev-a"])

    def test_replace_with_same_members_still_increments(self) -> None:
        self.create_group()
        result = self.service.replace_group_members(
            "grp-1", {"device_ids": ["dev-a", "dev-b"]}
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["device_ids"], ["dev-a", "dev-b"])

    def test_replace_with_expected_version(self) -> None:
        self.create_group()
        result = self.service.replace_group_members(
            "grp-1", {"device_ids": [], "expected_version": 1}
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["device_ids"], [])
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members(
                "grp-1", {"device_ids": [], "expected_version": 1}
            )
        self.assertEqual(ctx.exception.code, "group_version_conflict")
        self.assertEqual(ctx.exception.status, 409)

    def test_replace_unknown_group_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members("ghost", {"device_ids": []})
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_replace_with_unknown_device_leaves_group_unchanged(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.service.replace_group_members(
                "grp-1", {"device_ids": ["dev-a", "ghost"], "expected_version": 1}
            )
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)
        group = self.service.get_group("grp-1")
        self.assertEqual(group["version"], 1)
        self.assertEqual(group["device_ids"], ["dev-a", "dev-b"])

    def test_replace_keeps_revoked_devices_as_members(self) -> None:
        self.create_group()
        self.service.revoke_device("dev-b")
        result = self.service.replace_group_members(
            "grp-1", {"device_ids": ["dev-a", "dev-b"]}
        )
        self.assertEqual(result["device_ids"], ["dev-a", "dev-b"])

    def test_replace_invalid_payloads(self) -> None:
        self.create_group()
        bad_payloads = [
            "not-an-object",
            {},
            {"device_ids": [], "extra": 1},
            {"device_ids": ["dev-a", "dev-a"]},
            {"device_ids": [1]},
            {"device_ids": "dev-a"},
            {"device_ids": [], "expected_version": -1},
            {"device_ids": [], "expected_version": "1"},
            {"device_ids": [], "expected_version": 1.0},
            {"device_ids": [], "expected_version": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.replace_group_members("grp-1", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 失败不改变组。
        self.assertEqual(self.service.get_group("grp-1")["version"], 1)

    def test_revoke_device_does_not_remove_membership(self) -> None:
        self.create_group()
        self.service.revoke_device("dev-a")
        self.assertEqual(
            self.service.get_group("grp-1")["device_ids"], ["dev-a", "dev-b"]
        )


class CommandBatchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.credentials = {}
        for device_id in ("dev-a", "dev-b", "dev-c"):
            device = self.service.register_device(
                {"device_id": device_id, "display_name": device_id}
            )
            self.credentials[device_id] = device["credential"]

    def create_group(self, group_id="grp-1", device_ids=("dev-a", "dev-b")):
        return self.service.create_group(
            {"group_id": group_id, "device_ids": list(device_ids)}
        )

    def dispatch(self, group_id="grp-1", request_id="req-1",
                 name="reboot", payload=None, ttl=60, expected=None):
        body = {
            "request_id": request_id,
            "command_name": name,
            "payload": {"delay": 1} if payload is None else payload,
            "ttl_seconds": ttl,
        }
        if expected is not None:
            body["expected_group_version"] = expected
        return self.service.create_command_batch(group_id, body)

    def create_session(self, device_id):
        return self.service.create_session({
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": f"cli-{device_id}",
            "keepalive_seconds": 30,
        })

    def poll(self, session):
        return self.service.poll_commands(session["session_id"], {
            "session_token": session["session_token"],
            "max_commands": 10,
        })

    def ack(self, session, command_id, status="succeeded"):
        return self.service.ack_command(
            session["session_id"], command_id,
            {"session_token": session["session_token"],
             "status": status, "result": {"exit_code": 0}},
        )

    def force_expire_command(self, command_id):
        self.service._commands[command_id]["expires_at"] = (
            _utc_now() - timedelta(seconds=1)
        )

    # ------------------------------------------------------------------
    # 受理与快照
    # ------------------------------------------------------------------

    def test_dispatch_creates_commands_in_member_order(self) -> None:
        self.create_group(device_ids=("dev-c", "dev-a"))
        result = self.dispatch()
        self.assertTrue(result["batch_id"])
        self.assertEqual(result["group_version"], 1)
        self.assertEqual(
            [item["device_id"] for item in result["commands"]],
            ["dev-c", "dev-a"],
        )
        self.assertEqual(len(result["commands"]), 2)
        self.assertTrue(all(item["command_id"] for item in result["commands"]))
        self.assertEqual(
            set(result), {"batch_id", "group_version", "commands"}
        )
        # 受理视图的每项仅含 device_id 与 command_id。
        self.assertEqual(
            set(result["commands"][0]), {"device_id", "command_id"}
        )
        # 子命令确实进入既有命令存储，状态为 queued。
        for item in result["commands"]:
            snapshot = self.service.get_command(
                item["device_id"], item["command_id"]
            )
            self.assertEqual(snapshot["status"], "queued")
            self.assertEqual(snapshot["command_name"], "reboot")
            self.assertEqual(snapshot["payload"], {"delay": 1})
            self.assertEqual(snapshot["ttl_seconds"], 60)

    def test_batch_ids_are_unique_and_unpredictable(self) -> None:
        self.create_group(device_ids=())
        ids = {self.dispatch(request_id=f"req-{i}")["batch_id"]
               for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_dispatch_to_empty_group_creates_zero_item_batch(self) -> None:
        self.create_group(device_ids=())
        result = self.dispatch()
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["group_version"], 1)
        fetched = self.service.get_command_batch("grp-1", result["batch_id"])
        self.assertEqual(fetched["commands"], [])
        self.assertEqual(fetched["status_counts"], {
            "queued": 0, "delivered": 0, "succeeded": 0, "failed": 0,
            "expired": 0, "cancelled": 0,
        })

    def test_dispatch_uses_snapshot_of_members_at_acceptance(self) -> None:
        self.create_group()
        first = self.dispatch(request_id="req-1")
        self.assertEqual(len(first["commands"]), 2)
        self.service.replace_group_members(
            "grp-1", {"device_ids": ["dev-b"], "expected_version": 1}
        )
        second = self.dispatch(request_id="req-2")
        self.assertEqual(second["group_version"], 2)
        self.assertEqual(
            [item["device_id"] for item in second["commands"]], ["dev-b"]
        )
        old = self.service.get_command_batch("grp-1", first["batch_id"])
        self.assertEqual(
            [command["device_id"] for command in old["commands"]],
            ["dev-a", "dev-b"],
        )
        self.assertEqual(old["group_version"], 1)

    def test_dispatch_expected_group_version_conflict(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.dispatch(expected=99)
        self.assertEqual(ctx.exception.code, "group_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service._commands, {})

    def test_dispatch_unknown_group_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.dispatch(group_id="ghost")
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_dispatch_with_revoked_member_rejected(self) -> None:
        self.create_group()
        self.service.revoke_device("dev-a")
        with self.assertRaises(ServiceError) as ctx:
            self.dispatch()
        self.assertEqual(ctx.exception.code,
                         "group_contains_revoked_device")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service._commands, {})

    def test_dispatch_accepts_any_json_payload(self) -> None:
        self.create_group(device_ids=("dev-a",))
        for index, payload in enumerate(
            (None, True, 3, 1.5, "text", [1, 2], {"a": [None]})
        ):
            result = self.service.create_command_batch("grp-1", {
                "request_id": f"req-{index}",
                "command_name": "reboot",
                "payload": payload,
                "ttl_seconds": 60,
            })
            command_id = result["commands"][0]["command_id"]
            self.assertEqual(
                self.service.get_command("dev-a", command_id)["payload"],
                payload,
            )

    def test_dispatch_invalid_payloads(self) -> None:
        self.create_group(device_ids=())
        base = {"request_id": "req-x", "command_name": "reboot",
                "payload": None, "ttl_seconds": 60}
        bad_payloads = [
            "not-an-object",
            {},
            {**{k: v for k, v in base.items() if k != "request_id"}},
            {**{k: v for k, v in base.items() if k != "command_name"}},
            {**{k: v for k, v in base.items() if k != "payload"}},
            {**{k: v for k, v in base.items() if k != "ttl_seconds"}},
            {**base, "extra": 1},
            {**base, "request_id": ""},
            {**base, "request_id": "x" * 65},
            {**base, "request_id": 1},
            {**base, "request_id": True},
            {**base, "command_name": "bad name"},
            {**base, "command_name": ""},
            {**base, "command_name": 5},
            {**base, "ttl_seconds": 4},
            {**base, "ttl_seconds": 86401},
            {**base, "ttl_seconds": "60"},
            {**base, "ttl_seconds": True},
            {**base, "expected_group_version": -1},
            {**base, "expected_group_version": "1"},
            {**base, "expected_group_version": 1.0},
            {**base, "expected_group_version": False},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_command_batch("grp-1", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._commands, {})
        self.assertEqual(self.service._batches, {})

    # ------------------------------------------------------------------
    # request_id 幂等
    # ------------------------------------------------------------------

    def test_same_request_id_and_content_returns_original_batch(self) -> None:
        self.create_group()
        first = self.dispatch(request_id="dup", payload={"delay": 1})
        again = self.dispatch(request_id="dup", payload={"delay": 1})
        self.assertEqual(again["batch_id"], first["batch_id"])
        self.assertEqual(again["commands"], first["commands"])
        self.assertEqual(len(self.service._commands), 2)
        # JSON 数值相等（1 与 1.0）视为内容相同；布尔不与数字混同。
        same_number = self.dispatch(request_id="dup", payload={"delay": 1.0})
        self.assertEqual(same_number["batch_id"], first["batch_id"])
        self.assertEqual(len(self.service._commands), 2)

    def test_same_request_id_different_content_conflicts(self) -> None:
        self.create_group()
        self.dispatch(request_id="dup")
        variants = [
            {"name": "other"},
            {"ttl": 30},
            {"payload": {"delay": 2}},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                with self.assertRaises(ServiceError) as ctx:
                    self.dispatch(request_id="dup", **changes)
                self.assertEqual(ctx.exception.code, "batch_request_conflict")
                self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(self.service._commands), 2)

    def test_replay_ignores_expected_version_precondition(self) -> None:
        self.create_group()
        first = self.dispatch(request_id="dup")
        # 携带不符的预检查版本仍应返回原批次（预条件不参与内容比较）。
        again = self.dispatch(request_id="dup", expected=999)
        self.assertEqual(again["batch_id"], first["batch_id"])

    def test_request_id_scoped_per_group(self) -> None:
        self.create_group("grp-1", device_ids=())
        self.create_group("grp-2", device_ids=())
        first = self.dispatch("grp-1", request_id="dup")
        second = self.dispatch("grp-2", request_id="dup")
        self.assertNotEqual(first["batch_id"], second["batch_id"])

    def test_concurrent_identical_requests_create_single_batch(self) -> None:
        self.create_group()
        results: list[dict] = []

        def worker() -> None:
            results.append(self.dispatch(request_id="dup"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(len({r["batch_id"] for r in results}), 1)
        self.assertEqual(len(self.service._commands), 2)

    # ------------------------------------------------------------------
    # 批次查询与汇总
    # ------------------------------------------------------------------

    def test_get_batch_returns_current_snapshots_and_counts(self) -> None:
        self.create_group(device_ids=("dev-a", "dev-b", "dev-c"))
        batch = self.dispatch(ttl=5)
        ids = [item["command_id"] for item in batch["commands"]]

        session_a = self.create_session("dev-a")
        self.poll(session_a)
        self.ack(session_a, ids[0])
        session_b = self.create_session("dev-b")
        self.poll(session_b)  # delivered
        self.force_expire_command(ids[2])  # expired on read

        fetched = self.service.get_command_batch("grp-1", batch["batch_id"])
        self.assertEqual(
            [command["device_id"] for command in fetched["commands"]],
            ["dev-a", "dev-b", "dev-c"],
        )
        self.assertEqual(
            [command["command_id"] for command in fetched["commands"]], ids
        )
        statuses = [command["status"] for command in fetched["commands"]]
        self.assertEqual(statuses, ["succeeded", "delivered", "expired"])
        self.assertEqual(fetched["status_counts"], {
            "queued": 0, "delivered": 1, "succeeded": 1, "failed": 0,
            "expired": 1, "cancelled": 0,
        })

    def test_get_unknown_batch_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command_batch("grp-1", "nope")
        self.assertEqual(ctx.exception.code, "batch_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_batch_of_other_group_is_not_found(self) -> None:
        self.create_group("grp-1", device_ids=())
        self.create_group("grp-2", device_ids=())
        batch = self.dispatch("grp-1")
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_command_batch("grp-2", batch["batch_id"])
        self.assertEqual(ctx.exception.code, "batch_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_revoke_cancels_non_terminal_subcommands_in_batch(self) -> None:
        self.create_group()
        batch = self.dispatch()
        first = batch["commands"][0]["command_id"]
        session = self.create_session("dev-a")
        self.poll(session)
        self.ack(session, first)
        self.service.revoke_device("dev-a")
        self.service.revoke_device("dev-b")
        fetched = self.service.get_command_batch("grp-1", batch["batch_id"])
        self.assertEqual(
            [command["status"] for command in fetched["commands"]],
            ["succeeded", "cancelled"],
        )
        self.assertEqual(fetched["status_counts"]["cancelled"], 1)
        self.assertEqual(fetched["status_counts"]["succeeded"], 1)

    def test_subcommands_served_by_single_command_entrypoints(self) -> None:
        self.create_group(device_ids=("dev-a",))
        batch = self.dispatch()
        device_id, command_id = next(
            (item["device_id"], item["command_id"])
            for item in batch["commands"]
        )
        session = self.create_session(device_id)
        polled = self.poll(session)["commands"]
        self.assertEqual(polled[0]["command_id"], command_id)
        self.assertEqual(polled[0]["status"], "delivered")
        # 重领 dup 与确认沿用既有语义。
        self.assertTrue(self.poll(session)["commands"][0]["dup"])
        completed = self.ack(session, command_id)
        self.assertEqual(completed["status"], "succeeded")
        snapshot = self.service.get_command(device_id, command_id)
        self.assertEqual(snapshot["status"], "succeeded")
        fetched = self.service.get_command_batch("grp-1", batch["batch_id"])
        self.assertEqual(fetched["status_counts"]["succeeded"], 1)


if __name__ == "__main__":
    unittest.main()
