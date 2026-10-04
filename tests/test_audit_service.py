import unittest
from collections import deque

from devicefabric.service import Service, ServiceError


def rule_payload(rule_id="rule-1", enabled=True):
    return {
        "rule_id": rule_id,
        "topic_filter": "devices/+/telemetry",
        "enabled": enabled,
        "condition": {"path": ["temp"], "operator": "gt", "value": 30},
        "action": {"topic": "alerts/high", "payload": {"level": "high"}, "qos": 1},
    }


class AuditServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def query(self, query=""):
        return self.service.query_audit_events(query)

    def actions(self, query=""):
        return [event["action"] for event in self.query(query)["events"]]

    # ------------------------------------------------------------------
    # 设备生命周期事件
    # ------------------------------------------------------------------

    def test_device_lifecycle_appends_events_in_order(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.rotate_credential("dev-1")
        self.service.revoke_device("dev-1")
        events = self.query()["events"]
        self.assertEqual(
            [event["action"] for event in events],
            ["device.created", "device.credential_rotated", "device.revoked"],
        )
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        for event in events:
            self.assertEqual(event["resource_type"], "device")
            self.assertEqual(event["resource_id"], "dev-1")
            self.assertRegex(
                event["occurred_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
            )
            self.assertEqual(
                set(event),
                {"sequence", "occurred_at", "action", "resource_type", "resource_id"},
            )

    def test_idempotent_revoke_does_not_append(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.revoke_device("dev-1")
        self.service.revoke_device("dev-1")
        self.assertEqual(
            self.actions(), ["device.created", "device.revoked"]
        )

    def test_failed_requests_do_not_append(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        with self.assertRaises(ServiceError):
            self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        with self.assertRaises(ServiceError):
            self.service.rotate_credential("ghost")
        with self.assertRaises(ServiceError):
            self.service.revoke_device("ghost")
        with self.assertRaises(ServiceError):
            self.service.register_device({"device_id": "bad id", "display_name": "x"})
        self.assertEqual(self.actions(), ["device.created"])

    def test_reads_do_not_append(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.get_device("dev-1")
        self.service.list_rules()
        self.query()
        self.assertEqual(self.actions(), ["device.created"])

    # ------------------------------------------------------------------
    # 规则事件
    # ------------------------------------------------------------------

    def test_rule_lifecycle_appends_events(self) -> None:
        self.service.create_rule(rule_payload())
        self.service.set_rule_enabled("rule-1", {"enabled": False})
        self.service.set_rule_enabled("rule-1", {"enabled": True})
        self.service.delete_rule("rule-1")
        self.assertEqual(
            self.actions(),
            ["rule.created", "rule.enabled_changed",
             "rule.enabled_changed", "rule.deleted"],
        )
        for event in self.query()["events"]:
            self.assertEqual(event["resource_type"], "rule")
            self.assertEqual(event["resource_id"], "rule-1")

    def test_rule_enabled_without_change_does_not_append(self) -> None:
        self.service.create_rule(rule_payload(enabled=True))
        self.service.set_rule_enabled("rule-1", {"enabled": True})
        self.assertEqual(self.actions(), ["rule.created"])

    def test_failed_rule_operations_do_not_append(self) -> None:
        self.service.create_rule(rule_payload())
        with self.assertRaises(ServiceError):
            self.service.create_rule(rule_payload())
        with self.assertRaises(ServiceError):
            self.service.set_rule_enabled("ghost", {"enabled": False})
        with self.assertRaises(ServiceError):
            self.service.delete_rule("ghost")
        self.assertEqual(self.actions(), ["rule.created"])

    # ------------------------------------------------------------------
    # 设备组事件
    # ------------------------------------------------------------------

    def test_group_lifecycle_appends_events(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-1"]})
        self.service.replace_group_members("grp-1", {"device_ids": []})
        events = self.query()["events"]
        self.assertEqual(
            [event["action"] for event in events],
            ["device.created", "group.created", "group.members_replaced"],
        )
        group_events = events[1:]
        for event in group_events:
            self.assertEqual(event["resource_type"], "group")
            self.assertEqual(event["resource_id"], "grp-1")

    def test_identical_members_replacement_still_appends(self) -> None:
        # 成员替换即使内容相同版本也递增，属于状态变更，应记审计。
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-1"]})
        self.service.replace_group_members("grp-1", {"device_ids": ["dev-1"]})
        self.assertEqual(
            self.actions(),
            ["device.created", "group.created", "group.members_replaced"],
        )

    def test_failed_group_operations_do_not_append(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-1"]})
        with self.assertRaises(ServiceError):
            self.service.create_group({"group_id": "grp-1", "device_ids": []})
        with self.assertRaises(ServiceError):
            self.service.replace_group_members("grp-1", {"device_ids": ["ghost"]})
        with self.assertRaises(ServiceError):
            self.service.replace_group_members(
                "grp-1", {"device_ids": [], "expected_version": 99}
            )
        self.assertEqual(
            self.actions(), ["device.created", "group.created"]
        )

    # ------------------------------------------------------------------
    # 固件事件
    # ------------------------------------------------------------------

    def make_release(self, release_id="rel-1"):
        return self.service.create_firmware_release(
            {
                "release_id": release_id,
                "version": "1.0.0",
                "download_url": "https://example.com/fw.bin",
                "sha256": "a" * 64,
            }
        )

    def test_firmware_release_and_rollout_append_events(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-1"]})
        self.make_release()
        rollout = self.service.create_firmware_rollout("grp-1", {"release_id": "rel-1"})
        events = self.query()["events"]
        self.assertEqual(
            [event["action"] for event in events],
            [
                "device.created",
                "group.created",
                "firmware_release.created",
                "firmware_rollout.created",
            ],
        )
        self.assertEqual(events[2]["resource_type"], "firmware_release")
        self.assertEqual(events[2]["resource_id"], "rel-1")
        self.assertEqual(events[3]["resource_type"], "firmware_rollout")
        self.assertEqual(events[3]["resource_id"], rollout["rollout_id"])

    def test_failed_rollout_does_not_append(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-1"]})
        self.make_release()
        self.service.revoke_device("dev-1")
        with self.assertRaises(ServiceError):
            self.service.create_firmware_rollout("grp-1", {"release_id": "rel-1"})
        with self.assertRaises(ServiceError):
            self.service.create_firmware_rollout("grp-1", {"release_id": "ghost"})
        with self.assertRaises(ServiceError):
            self.make_release()
        self.assertEqual(
            self.actions(),
            ["device.created", "group.created",
             "firmware_release.created", "device.revoked"],
        )

    # ------------------------------------------------------------------
    # 查询：分页与过滤
    # ------------------------------------------------------------------

    def register_devices(self, count: int) -> None:
        for index in range(count):
            self.service.register_device(
                {"device_id": f"dev-{index}", "display_name": str(index)}
            )

    def test_empty_log_returns_empty_page_with_zero_cursor(self) -> None:
        result = self.query()
        self.assertEqual(result, {"events": [], "next_after": 0})

    def test_default_limit_is_fifty(self) -> None:
        self.register_devices(60)
        result = self.query()
        self.assertEqual(len(result["events"]), 50)
        self.assertEqual(result["events"][0]["sequence"], 1)
        self.assertEqual(result["next_after"], 50)
        rest = self.query("after=50")
        self.assertEqual(len(rest["events"]), 10)
        self.assertEqual(rest["next_after"], 60)

    def test_after_returns_strictly_later_sequences(self) -> None:
        self.register_devices(5)
        result = self.query("after=2")
        self.assertEqual(
            [event["sequence"] for event in result["events"]], [3, 4, 5]
        )
        self.assertEqual(result["next_after"], 5)

    def test_limit_truncates_page(self) -> None:
        self.register_devices(5)
        result = self.query("limit=2")
        self.assertEqual(
            [event["sequence"] for event in result["events"]], [1, 2]
        )
        self.assertEqual(result["next_after"], 2)

    def test_empty_page_keeps_after_cursor(self) -> None:
        self.register_devices(3)
        result = self.query("after=3")
        self.assertEqual(result, {"events": [], "next_after": 3})

    def test_action_filter_is_exact(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.rotate_credential("dev-1")
        self.service.create_rule(rule_payload())
        result = self.query("action=device.created")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["action"], "device.created")
        self.assertEqual(result["events"][0]["resource_id"], "dev-1")
        # 未知 action 仅匹配不到事件，不是错误。
        self.assertEqual(self.query("action=nope")["events"], [])

    def test_resource_id_filter_is_exact(self) -> None:
        self.service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.service.register_device({"device_id": "dev-2", "display_name": "乙"})
        self.service.rotate_credential("dev-1")
        result = self.query("resource_id=dev-1")
        self.assertEqual(
            [event["action"] for event in result["events"]],
            ["device.created", "device.credential_rotated"],
        )

    def test_combined_filters_and_after(self) -> None:
        self.register_devices(3)
        self.service.rotate_credential("dev-0")
        self.service.rotate_credential("dev-1")
        result = self.query("after=3&action=device.credential_rotated&resource_id=dev-1")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["resource_id"], "dev-1")

    # ------------------------------------------------------------------
    # 查询：参数校验
    # ------------------------------------------------------------------

    def assert_invalid(self, query: str) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.query(query)
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_parameter_rejected(self) -> None:
        self.assert_invalid("foo=1")

    def test_duplicate_parameter_rejected(self) -> None:
        self.assert_invalid("after=1&after=2")
        self.assert_invalid("limit=1&limit=2")
        self.assert_invalid("action=device.created&action=device.revoked")

    def test_malformed_after_rejected(self) -> None:
        for query in ("after=", "after=-1", "after=1.5", "after=abc", "after=+1"):
            self.assert_invalid(query)

    def test_malformed_or_out_of_range_limit_rejected(self) -> None:
        for query in ("limit=", "limit=0", "limit=101", "limit=-5", "limit=abc"):
            self.assert_invalid(query)

    def test_after_and_limit_accept_large_valid_values(self) -> None:
        self.register_devices(2)
        self.assertEqual(self.query("after=0001")["events"][0]["sequence"], 2)
        result = self.query("after=999999")
        self.assertEqual(result, {"events": [], "next_after": 999999})
        self.assertEqual(len(self.query("limit=100")["events"]), 2)

    # ------------------------------------------------------------------
    # 保留窗口与游标过期
    # ------------------------------------------------------------------

    def test_evicted_cursor_returns_410(self) -> None:
        # 用小窗口模拟保留淘汰：sequence 继续增长，最老事件被挤出。
        self.service._audit_events = deque(maxlen=3)
        self.register_devices(5)
        with self.assertRaises(ServiceError) as ctx:
            self.query("after=1")
        self.assertEqual(ctx.exception.code, "audit_cursor_expired")
        self.assertEqual(ctx.exception.status, 410)

    def test_cursor_at_oldest_minus_one_is_valid(self) -> None:
        self.service._audit_events = deque(maxlen=3)
        self.register_devices(5)
        result = self.query("after=2")
        self.assertEqual(
            [event["sequence"] for event in result["events"]], [3, 4, 5]
        )

    def test_missing_after_reads_from_oldest_retained(self) -> None:
        self.service._audit_events = deque(maxlen=3)
        self.register_devices(5)
        result = self.query()
        self.assertEqual(
            [event["sequence"] for event in result["events"]], [3, 4, 5]
        )
        self.assertEqual(result["next_after"], 5)


if __name__ == "__main__":
    unittest.main()
