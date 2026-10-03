import os
import tempfile
import unittest

from devicefabric.service import Service, ServiceError


class TelemetryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.credentials = {"sensor-01": self.device["credential"]}
        self.session = self.create_session()

    def create_session(self, client_id="cli-1", device_id="sensor-01"):
        return self.service.create_session({
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": client_id,
            "keepalive_seconds": 30,
        })

    def register_device(self, device_id):
        device = self.service.register_device(
            {"device_id": device_id, "display_name": "设备"}
        )
        self.credentials[device_id] = device["credential"]
        return device

    def submit(self, request_id="req-1", points=None, session=None):
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1.5}
            ]
        if session is None:
            session = self.session
        return self.service.submit_telemetry(session["session_id"], {
            "session_token": session["session_token"],
            "request_id": request_id,
            "points": points,
        })

    def query(self, metric="temp", start="2025-12-31T12:00:00Z",
              end="2026-01-01T12:00:00Z", resolution="raw",
              device_id="sensor-01"):
        return self.service.query_telemetry(
            device_id,
            f"metric={metric}&start={start}&end={end}&resolution={resolution}",
        )

    def assert_error(self, code, status, fn, *args):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    # --------------------------------------------------------------
    # 写入与幂等
    # --------------------------------------------------------------

    def test_submit_returns_request_id_and_accepted_count(self) -> None:
        result = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1.5},
            {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 2},
        ])
        self.assertEqual(result, {"request_id": "req-1", "accepted_count": 2})

    def test_submit_normalizes_timestamp_to_utc(self) -> None:
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T08:00:00+08:00", "value": 1},
        ])
        result = self.query()
        self.assertEqual(
            result["points"],
            [{"timestamp": "2026-01-01T00:00:00Z", "value": 1}],
        )

    def test_submit_idempotent_same_content(self) -> None:
        points = [
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1.5},
            {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 2},
        ]
        first = self.submit(points=points)
        second = self.submit(points=points)
        self.assertEqual(first, second)
        # 不重复写入。
        self.assertEqual(len(self.query()["points"]), 2)

    def test_submit_idempotent_equivalent_content(self) -> None:
        # 同一时刻的不同时区写法与数值等价写法视为相同内容。
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
        ])
        result = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T08:00:00+08:00", "value": 1.0},
        ])
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(len(self.query()["points"]), 1)

    def test_submit_conflict_different_content(self) -> None:
        self.submit()
        self.assert_error(
            "telemetry_request_conflict", 409,
            self.submit, "req-1",
            [{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 9}],
        )

    def test_request_id_scoped_per_device(self) -> None:
        self.register_device("sensor-02")
        other_session = self.create_session(client_id="cli-2", device_id="sensor-02")
        self.submit()
        # 不同设备使用相同 request_id 互不影响。
        result = self.submit(
            points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 7}],
            session=other_session,
        )
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(self.query()["points"][0]["value"], 1.5)
        self.assertEqual(
            self.query(device_id="sensor-02")["points"][0]["value"], 7
        )

    def test_submit_session_errors(self) -> None:
        payload = {
            "session_token": self.session["session_token"],
            "request_id": "req-1",
            "points": [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1}
            ],
        }
        self.assert_error(
            "session_not_found", 404,
            self.service.submit_telemetry, "no-such-session", payload,
        )
        bad_token = dict(payload, session_token="wrong")
        self.assert_error(
            "invalid_session_token", 401,
            self.service.submit_telemetry, self.session["session_id"], bad_token,
        )
        self.service.revoke_device("sensor-01")
        self.assert_error(
            "session_not_online", 409,
            self.service.submit_telemetry, self.session["session_id"], payload,
        )

    def test_submit_validation_errors(self) -> None:
        base = {
            "session_token": self.session["session_token"],
            "request_id": "req-1",
            "points": [
                {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1}
            ],
        }
        bad_payloads = [
            None,
            [],
            {k: v for k, v in base.items() if k != "points"},
            dict(base, extra=1),
            dict(base, session_token=1),
            dict(base, request_id=""),
            dict(base, request_id="x" * 65),
            dict(base, request_id=1),
            dict(base, points={}),
            dict(base, points=[]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": 1}] * 501),
            dict(base, points=[1]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z"}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": 1, "extra": 2}]),
            dict(base, points=[{"metric": "bad metric", "timestamp": "2026-01-01T00:00:00Z",
                                "value": 1}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01 00:00:00Z",
                                "value": 1}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00",
                                "value": 1}]),
            dict(base, points=[{"metric": "temp", "timestamp": "not-a-time", "value": 1}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": True}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": "1"}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": float("nan")}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": float("inf")}]),
            dict(base, points=[{"metric": "temp", "timestamp": "2026-01-01T00:00:00Z",
                                "value": None}]),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                self.assert_error(
                    "invalid_request", 400,
                    self.service.submit_telemetry,
                    self.session["session_id"], payload,
                )
        # 全部失败后没有任何数据与幂等记录。
        self.assertEqual(self.query()["points"], [])
        result = self.submit()
        self.assertEqual(result["accepted_count"], 1)

    def test_submit_batch_is_atomic(self) -> None:
        points = [
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": "bad"},
        ]
        self.assert_error("invalid_request", 400, self.submit, "req-1", points)
        self.assertEqual(self.query()["points"], [])

    # --------------------------------------------------------------
    # 查询：raw
    # --------------------------------------------------------------

    def test_query_raw_orders_by_time_then_acceptance(self) -> None:
        self.submit(request_id="req-1", points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:02:00Z", "value": 3},
            {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 1},
        ])
        self.submit(request_id="req-2", points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 2},
        ])
        result = self.query()
        self.assertEqual(result["resolution"], "raw")
        self.assertEqual(
            result["points"],
            [
                {"timestamp": "2026-01-01T00:01:00Z", "value": 1},
                {"timestamp": "2026-01-01T00:01:00Z", "value": 2},
                {"timestamp": "2026-01-01T00:02:00Z", "value": 3},
            ],
        )

    def test_query_range_includes_start_excludes_end(self) -> None:
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-01-01T01:00:00Z", "value": 2},
        ])
        result = self.query(start="2026-01-01T00:00:00Z", end="2026-01-01T01:00:00Z")
        self.assertEqual([p["value"] for p in result["points"]], [1])

    def test_query_filters_by_metric(self) -> None:
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
            {"metric": "humidity", "timestamp": "2026-01-01T00:00:00Z", "value": 2},
        ])
        self.assertEqual(len(self.query(metric="temp")["points"]), 1)
        self.assertEqual(self.query(metric="humidity")["points"][0]["value"], 2)
        self.assertEqual(self.query(metric="missing")["points"], [])

    def test_query_empty_result(self) -> None:
        result = self.query()
        self.assertEqual(result["points"], [])
        result = self.query(resolution="60")
        self.assertEqual(result["buckets"], [])

    def test_query_device_not_found(self) -> None:
        self.assert_error(
            "device_not_found", 404, self.query, "temp",
            "2025-12-31T12:00:00Z", "2026-01-01T12:00:00Z", "raw", "no-such-device",
        )

    def test_query_revoked_device_still_readable(self) -> None:
        self.submit()
        self.service.revoke_device("sensor-01")
        result = self.query()
        self.assertEqual(len(result["points"]), 1)

    def test_query_validation_errors(self) -> None:
        bad_queries = [
            "",
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z",
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"
            "&resolution=raw&extra=1",
            "metric=temp&metric=temp&start=2026-01-01T00:00:00Z"
            "&end=2026-01-02T00:00:00Z&resolution=raw",
            "metric=&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&resolution=raw",
            "metric=bad%20metric&start=2026-01-01T00:00:00Z"
            "&end=2026-01-02T00:00:00Z&resolution=raw",
            "metric=temp&start=2026-01-01&end=2026-01-02T00:00:00Z&resolution=raw",
            "metric=temp&start=2026-01-01T00:00:00&end=2026-01-02T00:00:00Z"
            "&resolution=raw",
            "metric=temp&start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z"
            "&resolution=raw",
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-01T00:00:00Z"
            "&resolution=raw",
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"
            "&resolution=30",
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"
            "&resolution=RAW",
            # raw 超过 24 小时。
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:01Z"
            "&resolution=raw",
            # 降采样超过 31 天。
            "metric=temp&start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:01Z"
            "&resolution=60",
        ]
        for query in bad_queries:
            with self.subTest(query=query):
                self.assert_error(
                    "invalid_request", 400,
                    self.service.query_telemetry, "sensor-01", query,
                )

    def test_query_range_limits_inclusive_boundary(self) -> None:
        # 恰好 24 小时的 raw 与恰好 31 天的降采样均可接受。
        result = self.service.query_telemetry(
            "sensor-01",
            "metric=temp&start=2026-01-01T00:00:00Z"
            "&end=2026-01-02T00:00:00Z&resolution=raw",
        )
        self.assertEqual(result["points"], [])
        result = self.service.query_telemetry(
            "sensor-01",
            "metric=temp&start=2026-01-01T00:00:00Z"
            "&end=2026-02-01T00:00:00Z&resolution=3600",
        )
        self.assertEqual(result["buckets"], [])

    # --------------------------------------------------------------
    # 查询：固定窗口降采样
    # --------------------------------------------------------------

    def test_downsample_aggregates_epoch_aligned_windows(self) -> None:
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:10Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-01-01T00:00:20Z", "value": 3},
            {"metric": "temp", "timestamp": "2026-01-01T00:00:40Z", "value": 2},
            {"metric": "temp", "timestamp": "2026-01-01T00:01:05Z", "value": 10},
        ])
        result = self.query(resolution="60")
        self.assertEqual(
            result["buckets"],
            [
                {"start": "2026-01-01T00:00:00Z", "count": 3,
                 "min": 1, "max": 3, "avg": 2.0, "last": 2},
                {"start": "2026-01-01T00:01:00Z", "count": 1,
                 "min": 10, "max": 10, "avg": 10.0, "last": 10},
            ],
        )

    def test_downsample_skips_empty_windows(self) -> None:
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-01-01T01:00:00Z", "value": 2},
        ])
        result = self.query(resolution="300")
        self.assertEqual(
            [bucket["start"] for bucket in result["buckets"]],
            ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
        )

    def test_downsample_last_uses_latest_point_in_window(self) -> None:
        self.submit(request_id="req-1", points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:30Z", "value": 5},
            {"metric": "temp", "timestamp": "2026-01-01T00:00:10Z", "value": 1},
        ])
        # 同时间戳时 last 取受理顺序最后一点。
        self.submit(request_id="req-2", points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:30Z", "value": 6},
        ])
        result = self.query(resolution="60")
        self.assertEqual(result["buckets"][0]["last"], 6)
        self.assertEqual(result["buckets"][0]["count"], 3)

    def test_downsample_window_alignment_not_query_bound(self) -> None:
        # 窗口按 Unix 纪元对齐，而不是按查询 start 对齐。
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-01-01T00:00:45Z", "value": 1},
        ])
        result = self.query(
            start="2026-01-01T00:00:30Z", end="2026-01-01T00:01:30Z",
            resolution="60",
        )
        self.assertEqual(result["buckets"][0]["start"], "2026-01-01T00:00:00Z")

    # --------------------------------------------------------------
    # 生命周期与持久化
    # --------------------------------------------------------------

    def test_credential_rotation_and_offline_keep_telemetry(self) -> None:
        self.submit()
        self.service.rotate_credential("sensor-01")
        self.assertEqual(len(self.query()["points"]), 1)
        # 会话因设备吊销离线后遥测仍保留。
        self.service.revoke_device("sensor-01")
        self.assertEqual(len(self.query()["points"]), 1)

    def test_in_memory_data_cleared_without_path(self) -> None:
        self.submit()
        fresh = Service()
        self.assertNotIn("sensor-01", fresh._devices)

    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "telemetry.json")
            service = Service(telemetry_path=path)
            device = service.register_device(
                {"device_id": "sensor-01", "display_name": "传感器"}
            )
            session = service.create_session({
                "device_id": "sensor-01",
                "credential": device["credential"],
                "client_id": "cli-1",
                "keepalive_seconds": 30,
            })
            service.submit_telemetry(session["session_id"], {
                "session_token": session["session_token"],
                "request_id": "req-1",
                "points": [
                    {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
                    {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 2},
                ],
            })
            # 模拟重启：新 Service 从同一路径恢复数据与幂等记录。
            restored = Service(telemetry_path=path)
            restored_device = restored.register_device(
                {"device_id": "sensor-01", "display_name": "传感器"}
            )
            result = restored.query_telemetry(
                "sensor-01",
                "metric=temp&start=2025-12-31T12:00:00Z"
                "&end=2026-01-01T12:00:00Z&resolution=raw",
            )
            self.assertEqual(
                result["points"],
                [
                    {"timestamp": "2026-01-01T00:00:00Z", "value": 1},
                    {"timestamp": "2026-01-01T00:01:00Z", "value": 2},
                ],
            )
            # 幂等记录跨重启保留：相同内容返回原结果且不重复写入。
            session2 = restored.create_session({
                "device_id": "sensor-01",
                "credential": restored_device["credential"],
                "client_id": "cli-1",
                "keepalive_seconds": 30,
            })
            replay = restored.submit_telemetry(session2["session_id"], {
                "session_token": session2["session_token"],
                "request_id": "req-1",
                "points": [
                    {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
                    {"metric": "temp", "timestamp": "2026-01-01T00:01:00Z", "value": 2},
                ],
            })
            self.assertEqual(replay, {"request_id": "req-1", "accepted_count": 2})
            result = restored.query_telemetry(
                "sensor-01",
                "metric=temp&start=2025-12-31T12:00:00Z"
                "&end=2026-01-01T12:00:00Z&resolution=raw",
            )
            self.assertEqual(len(result["points"]), 2)

    def test_storage_failure_returns_503_and_hides_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 目录不存在，写盘必然失败。
            path = os.path.join(tmp, "missing", "telemetry.json")
            service = Service(telemetry_path=path)
            device = service.register_device(
                {"device_id": "sensor-01", "display_name": "传感器"}
            )
            session = service.create_session({
                "device_id": "sensor-01",
                "credential": device["credential"],
                "client_id": "cli-1",
                "keepalive_seconds": 30,
            })
            payload = {
                "session_token": session["session_token"],
                "request_id": "req-1",
                "points": [
                    {"metric": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 1},
                ],
            }
            self.assert_error(
                "telemetry_storage_unavailable", 503,
                service.submit_telemetry, session["session_id"], payload,
            )
            # 数据与幂等记录均不可见。
            result = service.query_telemetry(
                "sensor-01",
                "metric=temp&start=2025-12-31T12:00:00Z"
                "&end=2026-01-01T12:00:00Z&resolution=raw",
            )
            self.assertEqual(result["points"], [])
            self.assert_error(
                "telemetry_storage_unavailable", 503,
                service.submit_telemetry, session["session_id"], payload,
            )


if __name__ == "__main__":
    unittest.main()
