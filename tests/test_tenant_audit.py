import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from devicefabric import (
    AuditCursorError,
    AuditStore,
    AuditValidationError,
    create_audit_store,
)

UTC = timezone.utc
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def at(hour, minute=0, second=0):
    return datetime(2026, 10, 4, hour, minute, second, tzinfo=UTC)


class AuditWriteTest(unittest.TestCase):
    def setUp(self):
        self.store = create_audit_store()

    def test_public_entry_creates_store(self):
        self.assertIsInstance(self.store, AuditStore)

    def test_append_returns_full_record(self):
        record = self.store.append(
            "tenant-a",
            "device.login",
            at(10),
            "success",
            device_id="dev-1",
            session_id="sess-1",
            correlation_id="corr-1",
            details={"ip": "10.0.0.1"},
        )
        self.assertEqual(
            record,
            {
                "sequence": 1,
                "tenant_id": "tenant-a",
                "event_type": "device.login",
                "occurred_at": "2026-10-04T10:00:00Z",
                "result": "success",
                "device_id": "dev-1",
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "details": {"ip": "10.0.0.1"},
            },
        )

    def test_optional_fields_omitted_when_absent(self):
        record = self.store.append("t", "e", at(10), "ok")
        self.assertEqual(
            set(record),
            {"sequence", "tenant_id", "event_type", "occurred_at", "result"},
        )

    def test_sequences_are_per_tenant_and_never_reused(self):
        self.assertEqual(self.store.append("a", "e", at(10), "ok")["sequence"], 1)
        self.assertEqual(self.store.append("b", "e", at(10), "ok")["sequence"], 1)
        self.assertEqual(self.store.append("a", "e", at(10), "ok")["sequence"], 2)
        self.assertEqual(self.store.append("a", "e", at(10), "ok")["sequence"], 3)

    def test_returned_record_matches_query(self):
        record = self.store.append("t", "e", at(10), "ok", details={"k": 1})
        page = self.store.query("t")
        self.assertEqual(page["events"], [record])

    def test_string_timestamp_accepted_and_normalized(self):
        record = self.store.append(
            "t", "e", "2026-10-04T12:00:00+02:00", "ok"
        )
        self.assertEqual(record["occurred_at"], "2026-10-04T10:00:00Z")

    def test_mutation_of_details_after_write_does_not_leak(self):
        details = {"nested": {"password": "p"}, "items": [1, 2]}
        self.store.append("t", "e", at(10), "ok", details=details)
        details["nested"]["password"] = "changed"
        details["items"].append(3)
        details["new"] = True
        stored = self.store.query("t")["events"][0]["details"]
        self.assertEqual(
            stored, {"nested": {"password": "p"}, "items": [1, 2]}
        )

    def test_mutation_of_returned_record_does_not_leak(self):
        record = self.store.append("t", "e", at(10), "ok", details={"k": 1})
        record["details"]["k"] = 999
        self.assertEqual(
            self.store.query("t")["events"][0]["details"], {"k": 1}
        )

    def assert_invalid_write(self, **kwargs):
        args = {
            "tenant_id": "t",
            "event_type": "e",
            "occurred_at": at(10),
            "result": "ok",
        }
        args.update(kwargs)
        with self.assertRaises(AuditValidationError):
            self.store.append(**args)

    def test_empty_required_fields_rejected(self):
        for field in ("tenant_id", "event_type", "result"):
            for bad in ("", "   ", None, 42):
                self.assert_invalid_write(**{field: bad})
        # 失败写入不产生部分记录，序号不被消耗。
        self.assertEqual(self.store.query("t")["events"], [])
        self.assertEqual(self.store.append("t", "e", at(10), "ok")["sequence"], 1)

    def test_invalid_time_rejected(self):
        for bad in (
            "not-a-time",
            "2026-10-04 10:00:00",
            datetime(2026, 10, 4, 10, 0),  # 无时区
            12345,
            None,
        ):
            self.assert_invalid_write(occurred_at=bad)

    def test_naive_and_aware_datetime(self):
        self.assert_invalid_write(occurred_at=datetime(2026, 10, 4))
        record = self.store.append(
            "t", "e", datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=2))), "ok"
        )
        self.assertEqual(record["occurred_at"], "2026-10-04T10:00:00Z")

    def test_non_serializable_details_rejected(self):
        for bad in (
            {"fn": object()},
            {"set": {1, 2}},
            ["not", "a", "dict"],
            "text",
            7,
        ):
            self.assert_invalid_write(details=bad)
        self.assertEqual(self.store.query("t")["events"], [])


class AuditQueryTest(unittest.TestCase):
    def setUp(self):
        self.store = create_audit_store()
        for index in range(5):
            self.store.append(
                "tenant-a",
                "device.login" if index % 2 == 0 else "device.logout",
                at(10, index),
                "success" if index % 2 == 0 else "failure",
                device_id=f"dev-{index % 2}",
            )
        self.store.append("tenant-b", "device.login", at(11), "success")

    def test_tenant_isolation(self):
        page = self.store.query("tenant-b")
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["tenant_id"], "tenant-b")
        self.assertEqual(page["events"][0]["sequence"], 1)
        page = self.store.query("tenant-ghost")
        self.assertEqual(page, {"events": [], "next_cursor": None})

    def test_tenant_required(self):
        for bad in ("", "  ", None):
            with self.assertRaises(AuditValidationError):
                self.store.query(bad)

    def test_filters(self):
        page = self.store.query("tenant-a", device_id="dev-0")
        self.assertEqual([e["sequence"] for e in page["events"]], [1, 3, 5])
        page = self.store.query("tenant-a", event_type="device.logout")
        self.assertEqual([e["sequence"] for e in page["events"]], [2, 4])
        page = self.store.query("tenant-a", result="failure")
        self.assertEqual([e["sequence"] for e in page["events"]], [2, 4])
        page = self.store.query(
            "tenant-a", event_type="device.login", device_id="dev-0"
        )
        self.assertEqual([e["sequence"] for e in page["events"]], [1, 3, 5])

    def test_time_range_filters_inclusive(self):
        page = self.store.query("tenant-a", start=at(10, 1), end=at(10, 3))
        self.assertEqual([e["sequence"] for e in page["events"]], [2, 3, 4])
        page = self.store.query("tenant-a", start="2026-10-04T10:02:00Z")
        self.assertEqual([e["sequence"] for e in page["events"]], [3, 4, 5])

    def test_invalid_time_filter_rejected(self):
        with self.assertRaises(AuditValidationError):
            self.store.query("tenant-a", start="junk")
        with self.assertRaises(AuditValidationError):
            self.store.query("tenant-a", end=datetime(2026, 1, 1))

    def test_limit_validation(self):
        for bad in (0, -1, 1001, 1.5, "10", None, True):
            with self.assertRaises(AuditValidationError):
                self.store.query("tenant-a", limit=bad)
        self.assertEqual(len(self.store.query("tenant-a", limit=1)["events"]), 1)
        self.assertEqual(
            len(self.store.query("tenant-a", limit=1000)["events"]), 5
        )

    def test_pagination_round_trip(self):
        seen = []
        cursor = None
        while True:
            page = self.store.query("tenant-a", limit=2, cursor=cursor)
            seen.extend(e["sequence"] for e in page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, [1, 2, 3, 4, 5])

    def test_cursor_is_opaque_string(self):
        page = self.store.query("tenant-a", limit=2)
        self.assertIsInstance(page["next_cursor"], str)

    def test_snapshot_boundary_excludes_later_writes(self):
        page1 = self.store.query("tenant-a", limit=2)
        self.assertEqual([e["sequence"] for e in page1["events"]], [1, 2])
        # 翻页期间写入新事件：后续页不得混入。
        self.store.append("tenant-a", "device.login", at(12), "success")
        page2 = self.store.query("tenant-a", limit=2, cursor=page1["next_cursor"])
        page3 = self.store.query("tenant-a", limit=2, cursor=page2["next_cursor"])
        self.assertEqual([e["sequence"] for e in page2["events"]], [3, 4])
        self.assertEqual([e["sequence"] for e in page3["events"]], [5])
        self.assertIsNone(page3["next_cursor"])
        # 新查询则能看到新事件。
        fresh = self.store.query("tenant-a")
        self.assertEqual(len(fresh["events"]), 6)

    def test_cursor_corruption_rejected(self):
        page = self.store.query("tenant-a", limit=2)
        good = page["next_cursor"]
        for bad in (good[:-2] + "xx", "!!!", "", "aGVsbG8=", 123, good + "x"):
            with self.assertRaises(AuditCursorError):
                self.store.query("tenant-a", cursor=bad)

    def test_cursor_cross_tenant_rejected(self):
        cursor = self.store.query("tenant-a", limit=2)["next_cursor"]
        with self.assertRaises(AuditCursorError):
            self.store.query("tenant-b", cursor=cursor)

    def test_cursor_with_changed_filters_rejected(self):
        cursor = self.store.query("tenant-a", limit=2)["next_cursor"]
        with self.assertRaises(AuditCursorError):
            self.store.query("tenant-a", event_type="device.login", cursor=cursor)
        with self.assertRaises(AuditCursorError):
            self.store.query("tenant-a", start=at(9), cursor=cursor)
        # 相同筛选条件下游标仍然有效。
        cursor = self.store.query(
            "tenant-a", event_type="device.login", limit=1
        )["next_cursor"]
        page = self.store.query(
            "tenant-a", event_type="device.login", limit=1, cursor=cursor
        )
        self.assertEqual([e["sequence"] for e in page["events"]], [3])

    def test_cursor_beyond_snapshot_rejected(self):
        import base64

        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": 1,
                    "tenant": "tenant-a",
                    "filters": "0" * 64,
                    "snapshot": 2,
                    "after": 5,
                }
            ).encode()
        ).decode()
        with self.assertRaises(AuditCursorError):
            self.store.query("tenant-a", cursor=payload)

    def test_deterministic_cursors(self):
        first = self.store.query("tenant-a", limit=3)["next_cursor"]
        second = self.store.query("tenant-a", limit=3)["next_cursor"]
        self.assertEqual(first, second)

    def test_empty_page_for_large_cursor_position(self):
        page = self.store.query("tenant-a", limit=5)
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(len(page["events"]), 5)


class AuditExportTest(unittest.TestCase):
    def setUp(self):
        self.store = create_audit_store()

    def test_empty_export(self):
        result = self.store.export("tenant-ghost")
        self.assertEqual(result["content"], b"")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(result["sha256"], EMPTY_SHA256)

    def test_export_matches_query_order_and_content(self):
        self.store.append(
            "t", "login", at(10), "success",
            device_id="dev-1", details={"user": "甲"},
        )
        self.store.append("t", "logout", at(11), "failure")
        self.store.append("other", "login", at(10), "success")
        result = self.store.export("t")
        self.assertEqual(result["record_count"], 2)
        lines = result["content"].decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        events = [json.loads(line) for line in lines]
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events, self.store.query("t")["events"])
        self.assertEqual(
            result["sha256"], hashlib.sha256(result["content"]).hexdigest()
        )

    def test_export_applies_filters(self):
        self.store.append("t", "login", at(10), "success", device_id="dev-1")
        self.store.append("t", "logout", at(11), "failure", device_id="dev-2")
        result = self.store.export("t", device_id="dev-2")
        self.assertEqual(result["record_count"], 1)
        result = self.store.export("t", end=at(10))
        self.assertEqual(result["record_count"], 1)

    def test_export_redacts_sensitive_keys_recursively(self):
        details = {
            "password": "p1",
            "PASSWORD": "p2",
            "Token": "t1",
            "nested": {
                "secret": "s1",
                "AUTHORIZATION": "Bearer x",
                "credential": {"raw": "y"},
                "keep": "visible",
            },
            "items": [{"Password": "p3"}, "plain"],
        }
        self.store.append("t", "login", at(10), "success", details=details)
        result = self.store.export("t")
        exported = json.loads(result["content"].decode("utf-8").splitlines()[0])
        redacted = exported["details"]
        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertEqual(redacted["PASSWORD"], "[REDACTED]")
        self.assertEqual(redacted["Token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["secret"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["AUTHORIZATION"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["credential"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["keep"], "visible")
        self.assertEqual(redacted["items"][0]["Password"], "[REDACTED]")
        self.assertEqual(redacted["items"][1], "plain")
        # 存储与普通查询不受影响。
        stored = self.store.query("t")["events"][0]["details"]
        self.assertEqual(stored, details)

    def test_export_tenant_required(self):
        with self.assertRaises(AuditValidationError):
            self.store.export("")

    def test_export_is_deterministic(self):
        self.store.append("t", "e", at(10), "ok", details={"b": 1, "a": 2})
        first = self.store.export("t")
        second = self.store.export("t")
        self.assertEqual(first["content"], second["content"])
        self.assertEqual(first["sha256"], second["sha256"])


class ExistingBehaviorTest(unittest.TestCase):
    def test_service_does_not_touch_audit_store(self):
        from devicefabric.service import Service

        service = Service()
        store = create_audit_store()
        service.register_device({"device_id": "dev-1", "display_name": "甲"})
        self.assertEqual(store.query("dev-1")["events"], [])
        # 既有审计查询行为不变。
        events = service.query_audit_events("")["events"]
        self.assertEqual([e["action"] for e in events], ["device.created"])


if __name__ == "__main__":
    unittest.main()
