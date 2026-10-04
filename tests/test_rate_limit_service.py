import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from devicefabric.service import Service, ServiceError


class RateLimitServiceTest(unittest.TestCase):
    def make_service(self, publish=None, telemetry=None):
        return Service(
            publish_rate_limit=publish, telemetry_point_rate_limit=telemetry
        )

    def setUp(self) -> None:
        self.service = self.make_service()
        self.credentials = {}

    def register_device(self, service, device_id):
        device = service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )
        self.credentials[id(service), device_id] = device["credential"]
        return device

    def create_session(self, service, device_id, client_id="cli-1"):
        return service.create_session({
            "device_id": device_id,
            "credential": self.credentials[id(service), device_id],
            "client_id": client_id,
            "keepalive_seconds": 300,
        })

    def publish(self, service, session, topic="a/b", payload=1, **extra):
        body = {
            "session_token": session["session_token"],
            "topic": topic,
            "payload": payload,
        }
        body.update(extra)
        return service.publish_message(session["session_id"], body)

    def submit(self, service, session, request_id, count=1, value_base=0):
        return service.submit_telemetry(session["session_id"], {
            "session_token": session["session_token"],
            "request_id": request_id,
            "points": [
                {
                    "metric": "temp",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "value": value_base + index,
                }
                for index in range(count)
            ],
        })

    def assert_rate_limited(self, fn, *args, **kwargs):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, "rate_limit_exceeded")
        self.assertEqual(ctx.exception.status, 429)
        retry_after = int(ctx.exception.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, 60)
        return ctx.exception

    def assert_error(self, code, status, fn, *args, **kwargs):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    # --------------------------------------------------------------
    # 限流关闭：未设置或为 0 时行为与基线一致
    # --------------------------------------------------------------

    def test_disabled_by_default_allows_unlimited_publish_and_telemetry(self):
        self.register_device(self.service, "dev-1")
        session = self.create_session(self.service, "dev-1")
        for _ in range(50):
            self.publish(self.service, session)
        for index in range(10):
            self.submit(self.service, session, f"req-{index}", count=100)

    def test_zero_limit_disables_rate_limiting(self):
        service = self.make_service(publish=0, telemetry=0)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        for _ in range(20):
            self.publish(service, session)
        for index in range(5):
            self.submit(service, session, f"req-{index}", count=100)

    def test_env_vars_configure_limits(self):
        env = {
            "DEVICEFABRIC_PUBLISH_RATE_LIMIT": "1",
            "DEVICEFABRIC_TELEMETRY_POINT_RATE_LIMIT": "2",
        }
        with mock.patch.dict(os.environ, env):
            service = Service()
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.publish(service, session)
        self.assert_rate_limited(self.publish, service, session)
        self.submit(service, session, "req-1", count=2)
        self.assert_rate_limited(self.submit, service, session, "req-2")

    def test_env_var_zero_disables_limit(self):
        env = {
            "DEVICEFABRIC_PUBLISH_RATE_LIMIT": "0",
            "DEVICEFABRIC_TELEMETRY_POINT_RATE_LIMIT": "0",
        }
        with mock.patch.dict(os.environ, env):
            service = Service()
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        for _ in range(20):
            self.publish(service, session)
        for index in range(5):
            self.submit(service, session, f"req-{index}", count=100)

    # --------------------------------------------------------------
    # 发布限流
    # --------------------------------------------------------------

    def test_publish_limit_counts_per_device_across_sessions(self):
        service = self.make_service(publish=3)
        self.register_device(service, "dev-1")
        session_a = self.create_session(service, "dev-1", client_id="cli-a")
        session_b = self.create_session(service, "dev-1", client_id="cli-b")
        self.publish(service, session_a)
        self.publish(service, session_b)
        self.publish(service, session_a)
        # 同一设备的多个会话共享额度，第四个发布被拒绝。
        self.assert_rate_limited(self.publish, service, session_b)

    def test_publish_limit_isolated_between_devices(self):
        service = self.make_service(publish=1)
        self.register_device(service, "dev-1")
        self.register_device(service, "dev-2")
        session_one = self.create_session(service, "dev-1")
        session_two = self.create_session(service, "dev-2")
        self.publish(service, session_one)
        self.assert_rate_limited(self.publish, service, session_one)
        # 不同设备互不影响。
        self.publish(service, session_two)

    def test_publish_429_leaves_no_state_changes(self):
        service = self.make_service(publish=1)
        self.register_device(service, "dev-1")
        self.register_device(service, "dev-2")
        watcher = self.create_session(service, "dev-2")
        service.subscribe_topic(watcher["session_id"], {
            "session_token": watcher["session_token"],
            "topic_filter": "a/#",
        })
        service.create_rule({
            "rule_id": "rule-1",
            "topic_filter": "a/#",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq", "value": 1},
            "action": {"topic": "b/c", "payload": {"hit": True}, "qos": 0},
        })
        session = self.create_session(service, "dev-1")
        self.publish(service, session, payload={"v": 1}, retain=True)
        # 清空观察者队列，便于观察被拒发布不产生任何投递。
        service.poll_messages(watcher["session_id"], {
            "session_token": watcher["session_token"], "max_messages": 100,
        })
        self.assert_rate_limited(
            self.publish, service, session, payload={"v": 1}, retain=True
        )
        # 被拒发布不触发规则动作、不产生实时投递。
        polled = service.poll_messages(watcher["session_id"], {
            "session_token": watcher["session_token"], "max_messages": 100,
        })
        self.assertEqual(polled, {"messages": []})
        # 保留状态仍是第一次发布的值（未被覆盖或删除）。
        service.subscribe_topic(session["session_id"], {
            "session_token": session["session_token"], "topic_filter": "a/b",
        })
        replayed = service.poll_messages(session["session_id"], {
            "session_token": session["session_token"], "max_messages": 100,
        })
        retained = [m for m in replayed["messages"] if m.get("retained")]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["payload"], {"v": 1})

    def test_publish_rule_actions_do_not_consume_quota(self):
        service = self.make_service(publish=2)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        for index in range(3):
            service.create_rule({
                "rule_id": f"rule-{index}",
                "topic_filter": "a/#",
                "enabled": True,
                "condition": {"path": ["v"], "operator": "eq", "value": 1},
                "action": {"topic": "b/c", "payload": 1, "qos": 0},
            })
        # 每次发布触发三条规则动作，但只占用一个发布额度。
        self.publish(service, session, payload={"v": 1})
        self.publish(service, session, payload={"v": 1})
        self.assert_rate_limited(self.publish, service, session, payload={"v": 1})

    def test_failed_publish_attempts_do_not_consume_quota(self):
        service = self.make_service(publish=1)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        # 非法请求（400）不消耗额度。
        self.assert_error(
            "invalid_request", 400,
            service.publish_message,
            session["session_id"],
            {"session_token": session["session_token"], "topic": "bad//topic",
             "payload": 1},
        )
        # 错误令牌（401）不消耗额度。
        self.assert_error(
            "invalid_session_token", 401,
            service.publish_message,
            session["session_id"],
            {"session_token": "wrong-token", "topic": "a/b", "payload": 1},
        )
        # 未知会话（404）不消耗额度。
        self.assert_error(
            "session_not_found", 404,
            service.publish_message,
            "no-such-session",
            {"session_token": "x", "topic": "a/b", "payload": 1},
        )
        # 以上失败均未消耗额度，第一次成功发布仍可受理。
        self.publish(service, session)
        self.assert_rate_limited(self.publish, service, session)

    def test_offline_session_publish_does_not_consume_quota(self):
        service = self.make_service(publish=1)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        # 同设备同 client_id 重连使旧会话离线；离线会话发布 409 不消耗额度。
        replacement = self.create_session(service, "dev-1")
        self.assert_error(
            "session_not_online", 409, self.publish, service, session
        )
        self.publish(service, replacement)
        self.assert_rate_limited(self.publish, service, replacement)

    # --------------------------------------------------------------
    # 遥测限流
    # --------------------------------------------------------------

    def test_telemetry_limit_counts_points_atomically(self):
        service = self.make_service(telemetry=5)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.submit(service, session, "req-1", count=3)
        # 整批 3 点超过剩余 2 点额度：整批拒绝，不扣减。
        self.assert_rate_limited(self.submit, service, session, "req-2", count=3)
        # 剩余额度内的小批次仍可受理。
        self.submit(service, session, "req-2", count=2)
        self.assert_rate_limited(self.submit, service, session, "req-3", count=1)

    def test_telemetry_429_writes_no_data_or_idempotency_record(self):
        service = self.make_service(telemetry=2)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.submit(service, session, "req-1", count=2)
        self.assert_rate_limited(self.submit, service, session, "req-2", count=1)
        # 被拒批次未留下幂等记录：窗口重置后同 request_id 可重新提交。
        self._advance_one_minute(service)
        result = self.submit(service, session, "req-2", count=1)
        self.assertEqual(result, {"request_id": "req-2", "accepted_count": 1})
        # 数据点总数仅为成功受理的 3 点。
        queried = service.query_telemetry(
            "dev-1",
            "metric=temp&start=2025-12-31T12:00:00Z"
            "&end=2026-01-01T12:00:00Z&resolution=raw",
        )
        self.assertEqual(len(queried["points"]), 3)

    def test_telemetry_idempotent_replay_bypasses_limit(self):
        service = self.make_service(telemetry=2)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.submit(service, session, "req-1", count=2)
        # 相同 request_id 且内容相同的重试返回原 202 结果，不重复计数。
        result = self.submit(service, session, "req-1", count=2)
        self.assertEqual(result, {"request_id": "req-1", "accepted_count": 2})

    def test_telemetry_conflict_takes_priority_over_limit(self):
        service = self.make_service(telemetry=2)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.submit(service, session, "req-1", count=2)
        # 额度已尽，但内容不同的同 request_id 仍返回既有 409。
        self.assert_error(
            "telemetry_request_conflict", 409,
            self.submit, service, session, "req-1", count=2, value_base=100,
        )

    def test_telemetry_limit_shared_across_sessions_and_isolated(self):
        service = self.make_service(telemetry=2)
        self.register_device(service, "dev-1")
        self.register_device(service, "dev-2")
        session_a = self.create_session(service, "dev-1", client_id="cli-a")
        session_b = self.create_session(service, "dev-1", client_id="cli-b")
        other = self.create_session(service, "dev-2")
        self.submit(service, session_a, "req-1", count=1)
        self.submit(service, session_b, "req-2", count=1)
        self.assert_rate_limited(self.submit, service, session_a, "req-3")
        # 不同设备互不影响。
        self.submit(service, other, "req-1", count=2)

    def test_failed_telemetry_attempts_do_not_consume_quota(self):
        service = self.make_service(telemetry=2)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        # 非法请求（400）不消耗额度。
        self.assert_error(
            "invalid_request", 400,
            service.submit_telemetry,
            session["session_id"],
            {"session_token": session["session_token"], "request_id": "bad",
             "points": [{"metric": "temp",
                         "timestamp": "2026-01-01T00:00:00Z",
                         "value": True}]},
        )
        # 错误令牌（401）不消耗额度。
        self.assert_error(
            "invalid_session_token", 401,
            service.submit_telemetry,
            session["session_id"],
            {"session_token": "wrong-token", "request_id": "bad2",
             "points": [{"metric": "temp",
                         "timestamp": "2026-01-01T00:00:00Z", "value": 1}]},
        )
        self.submit(service, session, "req-1", count=2)
        self.assert_rate_limited(self.submit, service, session, "req-2")

    # --------------------------------------------------------------
    # 窗口边界与 Retry-After
    # --------------------------------------------------------------

    def _advance_one_minute(self, service):
        """把窗口计数拨到上一分钟，模拟 UTC 分钟边界跨越。"""
        for windows in (
            service._publish_rate_windows,
            service._telemetry_rate_windows,
        ):
            for device_id, (window, used) in list(windows.items()):
                windows[device_id] = (window - 1, used)

    def test_window_boundary_restores_full_quota(self):
        service = self.make_service(publish=1, telemetry=1)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        self.publish(service, session)
        self.submit(service, session, "req-1")
        self.assert_rate_limited(self.publish, service, session)
        self.assert_rate_limited(self.submit, service, session, "req-2")
        # 跨入下一个 UTC 分钟后立即恢复完整额度。
        self._advance_one_minute(service)
        self.publish(service, session)
        self.submit(service, session, "req-2")

    def test_retry_after_counts_up_to_next_utc_minute(self):
        cases = [
            (datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc), 60),
            (datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc), 60),
            (datetime(2026, 1, 1, 0, 0, 30, 500000, tzinfo=timezone.utc), 30),
            (datetime(2026, 1, 1, 0, 0, 59, 999999, tzinfo=timezone.utc), 1),
        ]
        for moment, expected in cases:
            self.assertEqual(Service._retry_after_seconds(moment), expected)

    def test_retry_after_header_matches_time_to_boundary(self):
        service = self.make_service(publish=1)
        self.register_device(service, "dev-1")
        session = self.create_session(service, "dev-1")
        moment = datetime(2026, 1, 1, 12, 34, 50, 250000, tzinfo=timezone.utc)
        with mock.patch(
            "devicefabric.service._utc_now", return_value=moment
        ):
            self.publish(service, session)
            exc = self.assert_rate_limited(self.publish, service, session)
        self.assertEqual(int(exc.headers["Retry-After"]), 10)


if __name__ == "__main__":
    unittest.main()
