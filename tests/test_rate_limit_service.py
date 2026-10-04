"""按设备固定窗口限流的服务层测试。"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from devicefabric.service import (
    PUBLISH_RATE_LIMIT_ENV,
    RATE_WINDOW_MICROSECONDS,
    TELEMETRY_POINT_RATE_LIMIT_ENV,
    RateLimitError,
    Service,
    ServiceError,
    _epoch_microseconds,
    _parse_rate_limit,
    _retry_after_for_window,
)

UTC = timezone.utc


class ParseRateLimitTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertIsNone(_parse_rate_limit(None))
        self.assertIsNone(_parse_rate_limit(""))
        self.assertIsNone(_parse_rate_limit("0"))
        self.assertIsNone(_parse_rate_limit("00"))
        self.assertIsNone(_parse_rate_limit("-1"))
        self.assertIsNone(_parse_rate_limit("abc"))
        self.assertIsNone(_parse_rate_limit("1.5"))
        self.assertEqual(_parse_rate_limit("1"), 1)
        self.assertEqual(_parse_rate_limit("100"), 100)

    def test_retry_after_range_and_boundaries(self) -> None:
        minute = RATE_WINDOW_MICROSECONDS
        # 窗口起点：整 60 秒。
        self.assertEqual(_retry_after_for_window(0), 60)
        # 窗口内 1 微秒：剩余 59.999999 秒向上取整为 60。
        self.assertEqual(_retry_after_for_window(1), 60)
        # 30 秒整点：30。
        self.assertEqual(_retry_after_for_window(30 * 1_000_000), 30)
        # 距下一边界 1 微秒：向上取整为 1。
        self.assertEqual(_retry_after_for_window(minute - 1), 1)
        # 任意位置结果均在 1..60。
        for micros in range(0, minute, 499_999):
            self.assertIn(_retry_after_for_window(micros), range(1, 61))


class RateLimitServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service(
            publish_rate_limit=2, telemetry_point_rate_limit=5
        )
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.session = self.connect("sensor-01", self.device["credential"])

    def connect(self, device_id, credential, client_id="cli-1"):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential,
            "client_id": client_id,
            "keepalive_seconds": 3600,
        })

    def register(self, device_id):
        return self.service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )

    def subscribe(self, session, topic_filter):
        return self.service.subscribe_topic(session["session_id"], {
            "session_token": session["session_token"],
            "topic_filter": topic_filter,
        })

    def poll(self, session, max_messages=100):
        return self.service.poll_messages(session["session_id"], {
            "session_token": session["session_token"],
            "max_messages": max_messages,
        })["messages"]

    def publish(self, session=None, topic="t/data", payload=1, qos=None,
                retain=None):
        session = session or self.session
        body = {"session_token": session["session_token"],
                "topic": topic, "payload": payload}
        if qos is not None:
            body["qos"] = qos
        if retain is not None:
            body["retain"] = retain
        return self.service.publish_message(session["session_id"], body)

    def submit(self, request_id, points=None, session=None):
        session = session or self.session
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                 "value": 1}
            ]
        return self.service.submit_telemetry(session["session_id"], {
            "session_token": session["session_token"],
            "request_id": request_id,
            "points": points,
        })

    def point(self, minute=0, value=1, metric="temp"):
        return {"metric": metric,
                "timestamp": f"2026-01-01T00:{minute:02d}:00Z",
                "value": value}

    def assert_rate_limited(self, fn, *args):
        with self.assertRaises(RateLimitError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.code, "rate_limit_exceeded")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn(ctx.exception.retry_after, range(1, 61))
        return ctx.exception

    def assert_error(self, code, status, fn, *args):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    # --------------------------------------------------------------
    # 发布限流
    # --------------------------------------------------------------

    def test_publish_quota_exhausted(self) -> None:
        self.publish()
        self.publish(topic="t/other")
        self.assert_rate_limited(self.publish)

    def test_publish_retry_after_uses_controlled_clock(self) -> None:
        with mock.patch(
            "devicefabric.service._utc_now",
            lambda: datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC),
        ):
            self.publish()
            self.publish()
            error = self.assert_rate_limited(self.publish)
            self.assertEqual(error.retry_after, 30)
        with mock.patch(
            "devicefabric.service._utc_now",
            lambda: datetime(2026, 1, 1, 0, 0, 59, tzinfo=UTC),
        ):
            error = self.assert_rate_limited(self.publish)
            self.assertEqual(error.retry_after, 1)

    def test_publish_window_resets_at_utc_minute_boundary(self) -> None:
        frozen = [datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)]
        with mock.patch("devicefabric.service._utc_now", lambda: frozen[0]):
            self.publish()
            self.publish()
            self.assert_rate_limited(self.publish)
            # 到达下一个 UTC 分钟边界立即恢复完整额度。
            frozen[0] = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
            self.publish()
            self.publish()
            self.assert_rate_limited(self.publish)

    def test_failed_publish_does_not_consume_quota(self) -> None:
        # 非法请求（字段校验失败）在入锁前拒绝，不消耗额度。
        self.assert_error(
            "invalid_request", 400,
            self.service.publish_message, self.session["session_id"],
            {"session_token": self.session["session_token"], "topic": "",
             "payload": 1},
        )
        # 错误令牌不消耗额度。
        self.assert_error(
            "invalid_session_token", 401,
            self.service.publish_message, self.session["session_id"],
            {"session_token": "wrong", "topic": "t", "payload": 1},
        )
        # 未知会话不消耗额度。
        self.assert_error(
            "session_not_found", 404,
            self.service.publish_message, "no-such",
            {"session_token": "x", "topic": "t", "payload": 1},
        )
        # 离线会话不消耗额度：同组合重连取代旧会话。
        replaced = self.session
        replacement = self.connect("sensor-01", self.device["credential"])
        self.assert_error(
            "session_not_online", 409,
            self.service.publish_message, replaced["session_id"],
            {"session_token": replaced["session_token"],
             "topic": "t", "payload": 1},
        )
        # 额度仍是完整的 2 个。
        self.publish(session=replacement)
        self.publish(session=replacement)
        self.assert_rate_limited(self.publish, replacement)

    def test_publish_quota_shared_across_sessions_and_client_ids(self) -> None:
        # 先占用一个额度，再建立另一 client_id 的在线会话。
        self.publish(session=self.session)
        second = self.connect("sensor-01", self.device["credential"],
                              client_id="cli-2")
        self.publish(session=second)
        # 同 client_id 重连后的新会话共享同一设备额度，旧会话被取代。
        third = self.connect("sensor-01", self.device["credential"],
                             client_id="cli-1")
        self.assert_rate_limited(self.publish, third)

    def test_publish_quota_isolated_per_device(self) -> None:
        other_device = self.register("sensor-02")
        other = self.connect("sensor-02", other_device["credential"],
                             client_id="cli-9")
        self.publish()
        self.publish()
        self.assert_rate_limited(self.publish)
        # 不同设备互不影响，仍有完整额度。
        self.publish(session=other)
        self.publish(session=other)
        self.assert_rate_limited(self.publish, other)

    def test_blocked_publish_leaves_queues_retain_and_rules_untouched(
        self,
    ) -> None:
        # 订阅者 B 在线订阅 t 与规则动作主题 a/out。
        device_b = self.register("sensor-02")
        session_b = self.connect("sensor-02", device_b["credential"],
                                 client_id="sub")
        self.subscribe(session_b, "t")
        self.subscribe(session_b, "a/out")
        self.service.create_rule({
            "rule_id": "r1",
            "topic_filter": "t",
            "enabled": True,
            "condition": {"path": ["go"], "operator": "eq", "value": True},
            "action": {"topic": "a/out", "payload": {"x": 1}, "qos": 1},
        })
        # 用无订阅、无规则、无保留的发布占满该设备的发布额度。
        self.publish(topic="z", payload=0)
        self.publish(topic="z", payload=0)

        # 被限流的发布：原消息、保留状态、规则动作均不得产生变化。
        self.assert_rate_limited(
            self.publish, self.session, "t", {"go": True}, 1, True
        )
        # B 的实时队列没有原消息，也没有动作消息。
        self.assertEqual(self.poll(session_b), [])
        # 保留存储未写入：新订阅 t 不回放任何消息。
        session_c = self.connect("sensor-02", device_b["credential"],
                                 client_id="sub-2")
        self.subscribe(session_c, "t")
        self.assertEqual(self.poll(session_c), [])

    def test_accepted_publish_charges_once_including_rule_actions(self) -> None:
        # 对照：一次命中规则的成功发布只占一个发布额度（动作不另计）。
        device_b = self.register("sensor-02")
        session_b = self.connect("sensor-02", device_b["credential"],
                                 client_id="sub")
        self.subscribe(session_b, "t")
        self.subscribe(session_b, "a/out")
        self.service.create_rule({
            "rule_id": "r1",
            "topic_filter": "t",
            "enabled": True,
            "condition": {"path": ["go"], "operator": "eq", "value": True},
            "action": {"topic": "a/out", "payload": {"x": 1}, "qos": 0},
        })
        self.publish(topic="t", payload={"go": True})
        # 原消息 + 动作消息各投递一份。
        self.assertEqual(len(self.poll(session_b)), 2)
        # 第二次发布仍在额度内，第三次才被限流，证明动作未额外计费。
        self.publish(topic="z", payload=0)
        self.assert_rate_limited(self.publish)

    def test_blocked_publish_does_not_enqueue_offline_queue(self) -> None:
        device_b = self.register("sensor-02")
        session_b = self.service.create_session({
            "device_id": "sensor-02",
            "credential": device_b["credential"],
            "client_id": "persist",
            "keepalive_seconds": 3600,
            "clean_start": False,
        })
        self.subscribe(session_b, "off")
        # 同组合短 keepalive 重连：共享持久订阅，旧会话被取代。
        current = self.service.create_session({
            "device_id": "sensor-02",
            "credential": device_b["credential"],
            "client_id": "persist",
            "keepalive_seconds": 5,
            "clean_start": False,
        })
        # 将当前在线会话推进到超时：用未来时间访问它即转为 expired。
        with mock.patch(
            "devicefabric.service._utc_now",
            lambda: datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC),
        ):
            self.assert_error(
                "session_not_online", 409,
                self.service.poll_messages, current["session_id"],
                {"session_token": current["session_token"],
                 "max_messages": 10},
            )
        # 占满发布额度后尝试向离线持久会话发布 QoS 1：被 429 拒绝。
        self.publish(topic="z")
        self.publish(topic="z")
        self.assert_rate_limited(self.publish, self.session, "off", 1, 1)
        # 离线队列没有任何消息。
        routes = self.service._persistent_sessions[("sensor-02", "persist")]
        self.assertEqual(len(routes["queue"]), 0)

    # --------------------------------------------------------------
    # 遥测限流
    # --------------------------------------------------------------

    def test_telemetry_quota_charged_per_point(self) -> None:
        result = self.submit("r1", [self.point(0), self.point(1),
                                    self.point(2)])
        self.assertEqual(result["accepted_count"], 3)
        # 剩余 2 个点：整批 2 个点恰好放满。
        self.submit("r2", [self.point(3), self.point(4)])
        # 再来 1 个点即超限。
        self.assert_rate_limited(self.submit, "r3", [self.point(5)])

    def test_telemetry_batch_over_remaining_quota_writes_nothing(self) -> None:
        self.submit("r1", [self.point(0), self.point(1), self.point(2)])
        # 剩余 2 个点，整批 3 个点超限：不写数据也不建幂等记录。
        self.assert_rate_limited(
            self.submit, "r2", [self.point(3), self.point(4), self.point(5)]
        )
        # 因为没有幂等记录，同 request_id 的小批次重试作为新请求受理。
        result = self.submit("r2", [self.point(6), self.point(7)])
        self.assertEqual(result["accepted_count"], 2)

    def test_telemetry_batch_larger_than_limit_never_accepted(self) -> None:
        # 窗口额度 5，一批 6 个点永远放不下，且不会留下幂等记录。
        big = [self.point(i) for i in range(6)]
        self.assert_rate_limited(self.submit, "big", big)
        self.assert_rate_limited(self.submit, "big", big)

    def test_telemetry_idempotent_replay_not_charged(self) -> None:
        points = [self.point(0), self.point(1)]
        first = self.submit("r1", points)
        replay = self.submit("r1", points)
        self.assertEqual(first, replay)
        # 再占 3 个点，用满 5 个点额度。
        self.submit("r2", [self.point(2), self.point(3), self.point(4)])
        self.assert_rate_limited(self.submit, "r3", [self.point(5)])
        # 幂等重试在额度耗尽后仍返回原 202，且不改变受理结果。
        self.assertEqual(self.submit("r1", points), first)

    def test_telemetry_conflict_takes_priority_over_rate_limit(self) -> None:
        self.submit("r1", [self.point(0)])
        # 用后续批次把额度占满。
        self.submit("r2", [self.point(1), self.point(2), self.point(3),
                           self.point(4)])
        # 同 request_id 内容不同：优先返回既有 409，而不是 429，不计数。
        self.assert_error(
            "telemetry_request_conflict", 409,
            self.submit, "r1", [self.point(0, value=9)],
        )

    def test_failed_telemetry_does_not_consume_quota(self) -> None:
        good = [self.point(0)]
        # 非法请求 400。
        bad_body = {
            "session_token": self.session["session_token"],
            "request_id": "bad",
            "points": [{"metric": "temp", "timestamp": "not-a-time",
                        "value": 1}],
        }
        self.assert_error(
            "invalid_request", 400,
            self.service.submit_telemetry, self.session["session_id"], bad_body,
        )
        # 错误令牌 401。
        self.assert_error(
            "invalid_session_token", 401,
            self.service.submit_telemetry, self.session["session_id"],
            {"session_token": "wrong", "request_id": "x", "points": good},
        )
        # 未知会话 404。
        self.assert_error(
            "session_not_found", 404,
            self.service.submit_telemetry, "no-such",
            {"session_token": "x", "request_id": "x", "points": good},
        )
        # 离线会话 409。
        replaced = self.session
        replacement = self.connect("sensor-01", self.device["credential"])
        self.assert_error(
            "session_not_online", 409,
            self.service.submit_telemetry, replaced["session_id"],
            {"session_token": replaced["session_token"],
             "request_id": "x", "points": good},
        )
        # 额度仍是完整的 5 个点。
        self.assertEqual(
            self.submit("ok", [self.point(i) for i in range(5)],
                        session=replacement)["accepted_count"],
            5,
        )

    def test_telemetry_storage_failure_does_not_consume_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 目录不存在，写盘必然失败。
            path = os.path.join(tmp, "missing", "telemetry.json")
            service = Service(
                telemetry_path=path,
                publish_rate_limit=2,
                telemetry_point_rate_limit=2,
            )
            device = service.register_device(
                {"device_id": "sensor-01", "display_name": "传感器"}
            )
            session = service.create_session({
                "device_id": "sensor-01",
                "credential": device["credential"],
                "client_id": "cli-1",
                "keepalive_seconds": 3600,
            })
            payload = {
                "session_token": session["session_token"],
                "request_id": "r1",
                "points": [self.point(0)],
            }
            self.assert_error(
                "telemetry_storage_unavailable", 503,
                service.submit_telemetry, session["session_id"], payload,
            )
            # 若首次失败计了费，第二次会变成 429；应仍是 503。
            self.assert_error(
                "telemetry_storage_unavailable", 503,
                service.submit_telemetry, session["session_id"], payload,
            )

    def test_telemetry_quota_shared_and_isolated(self) -> None:
        other_device = self.register("sensor-02")
        other = self.connect("sensor-02", other_device["credential"],
                             client_id="cli-9")
        self.submit("a", [self.point(0) for _ in range(5)])
        self.assert_rate_limited(self.submit, "b", [self.point(1)])
        # 另一设备额度独立。
        self.assertEqual(
            self.submit("a", [self.point(0) for _ in range(5)],
                        session=other)["accepted_count"],
            5,
        )
        self.assert_rate_limited(self.submit, "b", [self.point(1)], other)
        # 同设备另一会话共享额度。
        second = self.connect("sensor-01", self.device["credential"],
                              client_id="cli-2")
        self.assert_rate_limited(self.submit, "c", [self.point(2)], second)

    def test_telemetry_window_resets_at_utc_minute_boundary(self) -> None:
        frozen = [datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)]
        with mock.patch("devicefabric.service._utc_now", lambda: frozen[0]):
            self.submit("r1", [self.point(i) for i in range(5)])
            self.assert_rate_limited(self.submit, "r2", [self.point(5)])
            frozen[0] = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
            self.assertEqual(
                self.submit("r2", [self.point(i) for i in range(5)])[
                    "accepted_count"],
                5,
            )
            self.assert_rate_limited(self.submit, "r3", [self.point(5)])

    # --------------------------------------------------------------
    # 并发：成功受理量不得突破配置值
    # --------------------------------------------------------------

    def test_concurrent_publishes_never_exceed_limit(self) -> None:
        service = Service(publish_rate_limit=100)
        device = service.register_device(
            {"device_id": "d", "display_name": "设备"}
        )
        sessions = [
            service.create_session({
                "device_id": "d",
                "credential": device["credential"],
                "client_id": f"cli-{i}",
                "keepalive_seconds": 3600,
            })
            for i in range(8)
        ]
        accepted = []
        rejected = []
        barrier = threading.Barrier(200)

        def worker(index):
            session = sessions[index % len(sessions)]
            barrier.wait()
            try:
                service.publish_message(session["session_id"], {
                    "session_token": session["session_token"],
                    "topic": "t", "payload": index,
                })
                accepted.append(index)
            except RateLimitError:
                rejected.append(index)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(200)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(accepted), 100)
        self.assertEqual(len(rejected), 100)

    def test_concurrent_telemetry_points_never_exceed_limit(self) -> None:
        service = Service(telemetry_point_rate_limit=100)
        device = service.register_device(
            {"device_id": "d", "display_name": "设备"}
        )
        sessions = [
            service.create_session({
                "device_id": "d",
                "credential": device["credential"],
                "client_id": f"cli-{i}",
                "keepalive_seconds": 3600,
            })
            for i in range(8)
        ]
        accepted_points = []
        rejected = []
        barrier = threading.Barrier(40)

        def worker(index):
            session = sessions[index % len(sessions)]
            points = [
                {"metric": "temp",
                 "timestamp": f"2026-01-01T00:{index:02d}:{slot:02d}Z",
                 "value": index}
                for slot in range(5)
            ]
            barrier.wait()
            try:
                result = service.submit_telemetry(session["session_id"], {
                    "session_token": session["session_token"],
                    "request_id": f"req-{index:03d}",
                    "points": points,
                })
                accepted_points.append(result["accepted_count"])
            except RateLimitError:
                rejected.append(index)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(accepted_points), 100)
        self.assertEqual(sum(accepted_points) + len(rejected) * 5, 200)

    # --------------------------------------------------------------
    # 环境变量与关闭语义
    # --------------------------------------------------------------

    def test_environment_configuration(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PUBLISH_RATE_LIMIT_ENV, None)
            os.environ.pop(TELEMETRY_POINT_RATE_LIMIT_ENV, None)
            self.assertIsNone(Service()._publish_rate_limit)
            self.assertIsNone(Service()._telemetry_point_rate_limit)

            os.environ[PUBLISH_RATE_LIMIT_ENV] = "0"
            os.environ[TELEMETRY_POINT_RATE_LIMIT_ENV] = "0"
            self.assertIsNone(Service()._publish_rate_limit)
            self.assertIsNone(Service()._telemetry_point_rate_limit)

            os.environ[PUBLISH_RATE_LIMIT_ENV] = "3"
            os.environ[TELEMETRY_POINT_RATE_LIMIT_ENV] = "7"
            enabled = Service()
            self.assertEqual(enabled._publish_rate_limit, 3)
            self.assertEqual(enabled._telemetry_point_rate_limit, 7)

    def test_disabled_limits_match_baseline(self) -> None:
        service = Service()
        device = service.register_device(
            {"device_id": "d", "display_name": "设备"}
        )
        session = service.create_session({
            "device_id": "d",
            "credential": device["credential"],
            "client_id": "cli",
            "keepalive_seconds": 3600,
        })
        # 远超任何可能配额的调用量在限流关闭时全部受理。
        for i in range(120):
            service.publish_message(session["session_id"], {
                "session_token": session["session_token"],
                "topic": "t", "payload": i,
            })
            service.submit_telemetry(session["session_id"], {
                "session_token": session["session_token"],
                "request_id": f"req-{i:03d}",
                "points": [{
                    "metric": "temp",
                    "timestamp": f"2026-02-01T00:{i % 60:02d}:00Z",
                    "value": i,
                }],
            })

    def test_usage_counters_are_process_local_not_persisted(self) -> None:
        # 限流计数只在进程内维护：服务重启即清空，不随遥测落盘恢复。
        self.assertEqual(self.service._publish_usage, {})
        self.publish()
        self.assertEqual(set(self.service._publish_usage), {"sensor-01"})
        fresh = Service(publish_rate_limit=2, telemetry_point_rate_limit=5)
        self.assertEqual(fresh._publish_usage, {})
        self.assertEqual(fresh._telemetry_usage, {})
        # sanity：窗口辅助基于 UTC 纪元微秒，窗口长度为 60 秒。
        self.assertGreater(
            _epoch_microseconds(datetime(2026, 1, 1, tzinfo=UTC)), 0
        )
        self.assertEqual(RATE_WINDOW_MICROSECONDS, 60 * 1_000_000)


if __name__ == "__main__":
    unittest.main()
