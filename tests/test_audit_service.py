import re
import unittest

from devicefabric.service import (
    AUDIT_RETENTION,
    AuditLog,
    Service,
    ServiceError,
)

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")

SHA_A = "a" * 64

VALID_RULE = {
    "rule_id": "rule-1",
    "topic_filter": "sensors/+/temp",
    "enabled": True,
    "condition": {"path": ["temp"], "operator": "gt", "value": 30},
    "action": {"topic": "alerts/high-temp", "payload": {"x": 1}, "qos": 1},
}


class AuditEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def events(self, query=""):
        return self.service.list_audit_events(
            f"limit=100{('&' + query) if query else ''}"
        )

    def test_full_lifecycle_appends_all_actions_in_order(self) -> None:
        device = self.service.register_device(
            {"device_id": "dev-a", "display_name": "设备甲"}
        )
        self.service.create_rule(VALID_RULE)
        # 与当前 enabled 相同：不追加事件。
        self.service.set_rule_enabled("rule-1", {"enabled": True})
        self.service.set_rule_enabled("rule-1", {"enabled": False})
        self.service.create_group({"group_id": "grp-1", "device_ids": ["dev-a"]})
        # 成员完全相同也构成提交：追加事件。
        self.service.replace_group_members(
            "grp-1", {"device_ids": ["dev-a"]}
        )
        release = self.service.create_firmware_release({
            "release_id": "rel-1",
            "version": "1.0.0",
            "download_url": "https://fw.example.com/rel-1.bin",
            "sha256": SHA_A,
        })
        rollout = self.service.create_firmware_rollout(
            "grp-1", {"release_id": "rel-1"}
        )
        self.service.rotate_credential("dev-a")
        self.service.revoke_device("dev-a")
        # 重复吊销幂等且未变更：不再追加。
        self.service.revoke_device("dev-a")

        events = self.events()["events"]
        actions = [event["action"] for event in events]
        self.assertEqual(actions, [
            "device.created",
            "rule.created",
            "rule.enabled_changed",
            "group.created",
            "group.members_replaced",
            "firmware_release.created",
            "firmware_rollout.created",
            "device.credential_rotated",
            "device.revoked",
        ])
        expected = [
            ("device", "dev-a"),
            ("rule", "rule-1"),
            ("rule", "rule-1"),
            ("group", "grp-1"),
            ("group", "grp-1"),
            ("firmware_release", "rel-1"),
            ("firmware_rollout", rollout["rollout_id"]),
            ("device", "dev-a"),
            ("device", "dev-a"),
        ]
        for index, (event, (resource_type, resource_id)) in enumerate(
            zip(events, expected), start=1
        ):
            self.assertEqual(set(event), {
                "sequence", "occurred_at", "action",
                "resource_type", "resource_id",
            })
            self.assertEqual(event["sequence"], index)
            self.assertEqual(event["resource_type"], resource_type)
            self.assertEqual(event["resource_id"], resource_id)
            self.assertTrue(RFC3339_RE.match(event["occurred_at"]))
        # 响应中不包含任何敏感字段。
        raw = repr(events)
        for secret in (device["credential"], "session_token", '"payload"', '"result"'):
            self.assertNotIn(secret, raw)
        self.assertEqual(release["release_id"], "rel-1")

    def test_failed_and_idempotent_requests_append_nothing(self) -> None:
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "设备甲"}
        )
        before = self.events()["events"]

        def expect_error(call, code):
            with self.assertRaises(ServiceError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, code)

        expect_error(
            lambda: self.service.register_device(
                {"device_id": "dev-a", "display_name": "重名"}
            ),
            "device_already_exists",
        )
        expect_error(lambda: self.service.rotate_credential("missing"), "device_not_found")
        expect_error(lambda: self.service.revoke_device("missing"), "device_not_found")
        expect_error(
            lambda: self.service.create_rule({"not": "valid"}), "invalid_request"
        )
        expect_error(
            lambda: self.service.create_group(
                {"group_id": "g", "device_ids": ["ghost"]}
            ),
            "device_not_found",
        )
        expect_error(
            lambda: self.service.create_firmware_release(
                {"release_id": "r", "version": "v",
                 "download_url": "u", "sha256": "bad"}
            ),
            "invalid_request",
        )
        self.assertEqual(self.events()["events"], before)

    def test_sequence_starts_at_one_and_is_strictly_increasing(self) -> None:
        for i in range(5):
            self.service.register_device(
                {"device_id": f"dev-{i}", "display_name": str(i)}
            )
        sequences = [event["sequence"] for event in self.events()["events"]]
        self.assertEqual(sequences, [1, 2, 3, 4, 5])

    def test_pagination_after_and_limit(self) -> None:
        for i in range(6):
            self.service.register_device(
                {"device_id": f"dev-{i}", "display_name": str(i)}
            )
        page1 = self.service.list_audit_events("limit=2")
        self.assertEqual([e["sequence"] for e in page1["events"]], [1, 2])
        self.assertEqual(page1["next_after"], 2)

        page2 = self.service.list_audit_events("after=2&limit=2")
        self.assertEqual([e["sequence"] for e in page2["events"]], [3, 4])
        self.assertEqual(page2["next_after"], 4)

        page3 = self.service.list_audit_events("after=4&limit=2")
        self.assertEqual([e["sequence"] for e in page3["events"]], [5, 6])
        self.assertEqual(page3["next_after"], 6)

        empty = self.service.list_audit_events("after=6&limit=2")
        self.assertEqual(empty["events"], [])
        # 空页 next_after 取传入的 after。
        self.assertEqual(empty["next_after"], 6)

    def test_default_limit_is_fifty(self) -> None:
        for i in range(60):
            self.service.register_device(
                {"device_id": f"dev-{i:02d}", "display_name": str(i)}
            )
        page = self.service.list_audit_events("")
        self.assertEqual(len(page["events"]), 50)
        self.assertEqual(page["next_after"], 50)

    def test_no_after_starts_from_oldest_and_empty_next_after_zero(self) -> None:
        # 空日志：无 after 时 next_after 为 0。
        empty = self.service.list_audit_events("")
        self.assertEqual(empty, {"events": [], "next_after": 0})

        for i in range(3):
            self.service.register_device(
                {"device_id": f"dev-{i}", "display_name": str(i)}
            )
        page = self.service.list_audit_events("limit=1")
        self.assertEqual(page["events"][0]["sequence"], 1)

    def test_action_filter_exact_match(self) -> None:
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "甲"}
        )
        self.service.create_rule(VALID_RULE)
        self.service.register_device(
            {"device_id": "dev-b", "display_name": "乙"}
        )
        page = self.service.list_audit_events(
            "action=device.created&limit=100"
        )
        self.assertEqual(
            [event["resource_id"] for event in page["events"]],
            ["dev-a", "dev-b"],
        )
        self.assertTrue(all(e["action"] == "device.created" for e in page["events"]))

    def test_resource_id_filter_exact_match(self) -> None:
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "甲"}
        )
        self.service.create_rule(VALID_RULE)
        self.service.set_rule_enabled("rule-1", {"enabled": False})
        page = self.service.list_audit_events(
            "resource_id=rule-1&limit=100"
        )
        self.assertEqual(
            [event["action"] for event in page["events"]],
            ["rule.created", "rule.enabled_changed"],
        )

    def test_filters_apply_before_limit_and_results_stay_ascending(self) -> None:
        # 交替产生 device.created 与 rule 无关事件，验证过滤后再截取。
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "甲"}
        )
        self.service.create_rule({**VALID_RULE, "rule_id": "r-a"})
        self.service.register_device(
            {"device_id": "dev-b", "display_name": "乙"}
        )
        page = self.service.list_audit_events("action=device.created&limit=1")
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["resource_id"], "dev-a")
        self.assertEqual(page["next_after"], 1)

    def test_invalid_query_parameters(self) -> None:
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "甲"}
        )
        bad_queries = [
            "after=-1",
            "after=abc",
            "after=1.5",
            "after=",
            "limit=0",
            "limit=101",
            "limit=abc",
            "after=1&after=2",
            "bogus=1",
            "limit=true",
        ]
        for query in bad_queries:
            with self.subTest(query=query):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.list_audit_events(query)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    def test_retention_and_cursor_expired(self) -> None:
        log = AuditLog()
        for sequence in range(1, AUDIT_RETENTION + 2):
            log.append_locked("device.created", f"dev-{sequence}")
        # 仅保留最近 10000 条，最老序号为 2，sequence 继续增长。
        self.assertEqual(len(log._events), AUDIT_RETENTION)
        self.assertEqual(log.oldest_sequence_locked(), 2)

        with self.assertRaises(ServiceError) as ctx:
            log.query_locked(0, 50, None, None)
        self.assertEqual(ctx.exception.code, "audit_cursor_expired")
        self.assertEqual(ctx.exception.status, 410)

        # 最老事件的前一序号是合法游标：after=1 从最老事件续读。
        page = log.query_locked(1, 2, None, None)
        self.assertEqual([event["sequence"] for event in page["events"]], [2, 3])
        self.assertEqual(page["next_after"], 3)

    def test_no_cursor_expired_when_after_omitted(self) -> None:
        log = AuditLog()
        for sequence in range(AUDIT_RETENTION + 2):
            log.append_locked("device.created", f"dev-{sequence}")
        page = log.query_locked(None, 5, None, None)
        # 未传 after：从现存最早事件（序号 3）读取。
        self.assertEqual([event["sequence"] for event in page["events"]], [3, 4, 5, 6, 7])
        self.assertEqual(page["next_after"], 7)

    def test_event_records_are_copies(self) -> None:
        self.service.register_device(
            {"device_id": "dev-a", "display_name": "甲"}
        )
        page = self.service.list_audit_events("")
        page["events"][0]["resource_id"] = "tampered"
        again = self.service.list_audit_events("")
        self.assertEqual(again["events"][0]["resource_id"], "dev-a")


if __name__ == "__main__":
    unittest.main()
