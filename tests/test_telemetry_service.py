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
        self.other = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号传感器"}
        )
        self.session = self.service.create_session({
            "device_id": "sensor-01",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })

    def submit(self, request_id="req-1", points=None, session=None):
        if points is None:
            points = [
                {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 21.5}
            ]
        if session is None:
            session = self.session
        return self.service.submit_telemetry(session["session_id"], {
            "session_token": session["session_token"],
            "request_id": request_id,
            "points": points,
        })

    def query(self, metric="temp", start="2026-10-01T00:00:00Z",
              end="2026-10-02T00:00:00Z", resolution="raw", device_id="sensor-01"):
        return self.service.query_telemetry(device_id, {
            "metric": metric,
            "start": start,
            "end": end,
            "resolution": resolution,
        })

    def assert_error(self, status, code, fn, *args, **kwargs):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.code, code)

    # --------------------------------------------------------------
    # 写入与校验
    # --------------------------------------------------------------

    def test_submit_success(self):
        result = self.submit(points=[
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 21.5},
            {"metric": "hum", "timestamp": "2026-10-01T00:00:01Z", "value": 40},
        ])
        self.assertEqual(result, {"request_id": "req-1", "accepted_count": 2})

    def test_submit_normalizes_timestamp_to_utc(self):
        self.submit(points=[
            {"metric": "temp", "timestamp": "2026-10-01T08:00:00+08:00", "value": 1}
        ])
        result = self.query()
        self.assertEqual(
            result["points"],
            [{"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}],
        )

    def test_submit_rejects_non_object_and_unknown_fields(self):
        self.assert_error(400, "invalid_request",
                          self.service.submit_telemetry, self.session["session_id"], [])
        self.assert_error(400, "invalid_request", self.service.submit_telemetry,
                          self.session["session_id"],
                          {"session_token": self.session["session_token"],
                           "request_id": "r", "points": [], "extra": 1})
        self.assert_error(400, "invalid_request", self.service.submit_telemetry,
                          self.session["session_id"],
                          {"session_token": self.session["session_token"],
                           "request_id": "r"})

    def test_submit_rejects_bad_request_id(self):
        for bad in ("", "x" * 65, 123, True, None):
            self.assert_error(400, "invalid_request", self.submit, bad)

    def test_submit_rejects_bad_points_container(self):
        for bad in ({}, "x", [], 1):
            self.assert_error(400, "invalid_request", self.submit, "req-1", bad)
        too_many = [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        ] * 501
        self.assert_error(400, "invalid_request", self.submit, "req-1", too_many)

    def test_submit_rejects_bad_point_items(self):
        base = {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        bad_items = [
            "not-an-object",
            {**base, "extra": 1},
            {k: v for k, v in base.items() if k != "value"},
            {**base, "metric": "bad metric!"},
            {**base, "metric": ""},
            {**base, "timestamp": "2026-10-01 00:00:00Z"},
            {**base, "timestamp": "2026-10-01T00:00:00"},  # 缺时区
            {**base, "timestamp": "not-a-time"},
            {**base, "timestamp": 123},
            {**base, "value": True},
            {**base, "value": "1"},
            {**base, "value": None},
            {**base, "value": float("nan")},
            {**base, "value": float("inf")},
            {**base, "value": float("-inf")},
        ]
        for index, item in enumerate(bad_items):
            self.assert_error(400, "invalid_request", self.submit, f"req-{index}", [item])
        # 全部拒绝后没有任何数据。
        self.assertEqual(self.query()["points"], [])

    def test_submit_is_atomic_across_points(self):
        points = [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "bad", "value": 2},
        ]
        self.assert_error(400, "invalid_request", self.submit, "req-1", points)
        self.assertEqual(self.query()["points"], [])
        # 同一 request_id 未被占用，可再次使用。
        result = self.submit("req-1", [points[0]])
        self.assertEqual(result["accepted_count"], 1)

    def test_submit_session_errors(self):
        points = [{"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}]
        self.assert_error(404, "session_not_found",
                          self.service.submit_telemetry, "no-such-session",
                          {"session_token": "x", "request_id": "r", "points": points})
        self.assert_error(401, "invalid_session_token",
                          self.service.submit_telemetry, self.session["session_id"],
                          {"session_token": "wrong", "request_id": "r", "points": points})
        # 会话被同组合重连替换后离线。
        self.service.create_session({
            "device_id": "sensor-01",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        self.assert_error(409, "session_not_online",
                          self.service.submit_telemetry, self.session["session_id"],
                          {"session_token": self.session["session_token"],
                           "request_id": "r", "points": points})

    # --------------------------------------------------------------
    # 幂等
    # --------------------------------------------------------------

    def test_idempotent_replay_returns_original_without_duplicates(self):
        points = [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-10-01T00:01:00Z", "value": 2},
        ]
        first = self.submit("req-1", points)
        replay = self.submit("req-1", points)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.query()["points"]), 2)

    def test_idempotent_replay_equivalent_content(self):
        # 数值 1 与 1.0、不同时区表示的同一时刻视为相同内容。
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        ])
        replay = self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T08:00:00+08:00", "value": 1.0}
        ])
        self.assertEqual(replay["accepted_count"], 1)
        self.assertEqual(len(self.query()["points"]), 1)

    def test_conflicting_request_id_rejected(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        ])
        self.assert_error(409, "telemetry_request_conflict", self.submit, "req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 2}
        ])
        # 冲突不写入任何数据。
        self.assertEqual(len(self.query()["points"]), 1)

    def test_request_id_scoped_per_device(self):
        other_session = self.service.create_session({
            "device_id": "sensor-02",
            "credential": self.other["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1}
        ])
        # 不同设备使用相同 request_id 与不同内容互不影响。
        result = self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 9}
        ], session=other_session)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(self.query(device_id="sensor-02")["points"][0]["value"], 9)

    # --------------------------------------------------------------
    # 查询
    # --------------------------------------------------------------

    def seed_points(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:03:00Z", "value": 3},
            {"metric": "temp", "timestamp": "2026-10-01T00:01:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-10-01T00:02:00Z", "value": 2},
        ])

    def test_raw_query_orders_by_time(self):
        self.seed_points()
        result = self.query()
        self.assertEqual(result["resolution"], "raw")
        self.assertEqual([p["value"] for p in result["points"]], [1, 2, 3])

    def test_raw_query_same_timestamp_uses_acceptance_order(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 2},
        ])
        self.submit("req-2", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
        ])
        result = self.query()
        self.assertEqual([p["value"] for p in result["points"]], [2, 1])

    def test_raw_query_interval_includes_start_excludes_end(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-10-01T01:00:00Z", "value": 2},
        ])
        result = self.query(start="2026-10-01T00:00:00Z", end="2026-10-01T01:00:00Z")
        self.assertEqual([p["value"] for p in result["points"]], [1])

    def test_query_filters_by_metric_and_device(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
            {"metric": "hum", "timestamp": "2026-10-01T00:00:00Z", "value": 2},
        ])
        result = self.query(metric="hum")
        self.assertEqual([p["value"] for p in result["points"]], [2])
        self.assertEqual(self.query(device_id="sensor-02")["points"], [])

    def test_query_no_data_returns_empty(self):
        result = self.query()
        self.assertEqual(result["points"], [])
        result = self.query(resolution="60")
        self.assertEqual(result["windows"], [])

    def test_query_unknown_device_404_but_revoked_queryable(self):
        self.assert_error(404, "device_not_found", self.query, device_id="no-such")
        self.submit("req-1")
        self.service.revoke_device("sensor-01")
        result = self.query()
        self.assertEqual(len(result["points"]), 1)

    def test_query_rejects_invalid_params(self):
        base = {"metric": "temp", "start": "2026-09-30T00:00:00Z",
                "end": "2026-10-02T00:00:00Z", "resolution": "raw"}
        for params in (
            {k: v for k, v in base.items() if k != "metric"},
            {k: v for k, v in base.items() if k != "start"},
            {k: v for k, v in base.items() if k != "end"},
            {k: v for k, v in base.items() if k != "resolution"},
            {**base, "extra": "x"},
            {**base, "metric": "bad metric!"},
            {**base, "start": "2026-09-30"},
            {**base, "end": "not-a-time"},
            {**base, "resolution": "1"},
            {**base, "resolution": "RAW"},
            {**base, "resolution": 60},
            # start 必须早于 end。
            {**base, "start": "2026-10-02T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
            {**base, "start": "2026-10-02T00:00:01Z", "end": "2026-10-02T00:00:00Z"},
        ):
            self.assert_error(400, "invalid_request",
                              self.service.query_telemetry, "sensor-01", params)

    def test_query_span_limits(self):
        # raw 恰好 24 小时可行，超出拒绝。
        self.query(start="2026-10-01T00:00:00Z", end="2026-10-02T00:00:00Z")
        self.assert_error(400, "invalid_request", self.query,
                          start="2026-10-01T00:00:00Z",
                          end="2026-10-02T00:00:01Z")
        # 降采样恰好 31 天可行，超出拒绝。
        self.query(resolution="300",
                   start="2026-10-01T00:00:00Z", end="2026-11-01T00:00:00Z")
        self.assert_error(400, "invalid_request", self.query, resolution="300",
                          start="2026-10-01T00:00:00Z",
                          end="2026-11-01T00:00:01Z")

    def test_downsampled_windows_aligned_to_epoch(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:10Z", "value": 10},
            {"metric": "temp", "timestamp": "2026-10-01T00:00:20Z", "value": 20},
            {"metric": "temp", "timestamp": "2026-10-01T00:01:05Z", "value": 5},
        ])
        result = self.query(resolution="60")
        self.assertEqual(len(result["windows"]), 2)
        first, second = result["windows"]
        self.assertEqual(first["start"], "2026-10-01T00:00:00Z")
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["min"], 10)
        self.assertEqual(first["max"], 20)
        self.assertEqual(first["avg"], 15)
        self.assertEqual(first["last"], 20)
        self.assertEqual(second["start"], "2026-10-01T00:01:00Z")
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["avg"], 5)

    def test_downsampled_omits_empty_windows_and_last_follows_acceptance(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-10-01T00:10:00Z", "value": 2},
        ])
        self.submit("req-2", [
            {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": 3},
        ])
        result = self.query(resolution="300")
        starts = [w["start"] for w in result["windows"]]
        self.assertEqual(starts, ["2026-10-01T00:00:00Z", "2026-10-01T00:10:00Z"])
        # 同时间戳两点，last 取受理顺序的最后一点。
        self.assertEqual(result["windows"][0]["last"], 3)
        self.assertEqual(result["windows"][0]["count"], 2)

    def test_downsampled_300_and_3600(self):
        self.submit("req-1", [
            {"metric": "temp", "timestamp": "2026-10-01T00:59:59Z", "value": 1},
            {"metric": "temp", "timestamp": "2026-10-01T01:00:00Z", "value": 2},
        ])
        result = self.query(resolution="3600")
        self.assertEqual([w["start"] for w in result["windows"]],
                         ["2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z"])

    def test_telemetry_survives_rotation_and_offline(self):
        self.submit("req-1")
        self.service.rotate_credential("sensor-01")
        # 旧会话因凭据轮换后无法重连，但既有遥测仍在。
        self.assertEqual(len(self.query()["points"]), 1)


class TelemetryPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "telemetry.jsonl")
        self._old = os.environ.get("DEVICEFABRIC_TELEMETRY_PATH")
        os.environ["DEVICEFABRIC_TELEMETRY_PATH"] = self.path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("DEVICEFABRIC_TELEMETRY_PATH", None)
        else:
            os.environ["DEVICEFABRIC_TELEMETRY_PATH"] = self._old

    def make_service_with_device(self):
        service = Service()
        device = service.register_device(
            {"device_id": "sensor-01", "display_name": "传感器"}
        )
        session = service.create_session({
            "device_id": "sensor-01",
            "credential": device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
        return service, device, session

    def submit(self, service, session, request_id="req-1", value=1):
        return service.submit_telemetry(session["session_id"], {
            "session_token": session["session_token"],
            "request_id": request_id,
            "points": [
                {"metric": "temp", "timestamp": "2026-10-01T00:00:00Z", "value": value}
            ],
        })

    def query(self, service):
        return service.query_telemetry("sensor-01", {
            "metric": "temp",
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-10-02T00:00:00Z",
            "resolution": "raw",
        })

    def test_data_and_idempotency_survive_restart(self):
        service, device, session = self.make_service_with_device()
        result = self.submit(service, session)
        self.assertEqual(result["accepted_count"], 1)

        # 模拟重启：同一路径构造全新 Service。
        restarted, device2, session2 = self.make_service_with_device()
        self.assertEqual(len(self.query(restarted)["points"]), 1)
        # 幂等记录同样保留：相同内容返回原结果且不重复写入。
        replay = self.submit(restarted, session2)
        self.assertEqual(replay, result)
        self.assertEqual(len(self.query(restarted)["points"]), 1)
        # 内容不同仍冲突。
        with self.assertRaises(ServiceError) as ctx:
            self.submit(restarted, session2, value=2)
        self.assertEqual(ctx.exception.code, "telemetry_request_conflict")

    def test_storage_failure_returns_503_and_hides_everything(self):
        # 指向一个已存在的目录：追加写入必然失败。
        os.environ["DEVICEFABRIC_TELEMETRY_PATH"] = self.tmpdir.name
        service, device, session = self.make_service_with_device()
        with self.assertRaises(ServiceError) as ctx:
            self.submit(service, session)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "telemetry_storage_unavailable")
        # 数据与幂等记录均不可见：查询为空，同 request_id 重试仍走写入路径。
        self.assertEqual(self.query(service)["points"], [])
        with self.assertRaises(ServiceError) as ctx:
            self.submit(service, session)
        self.assertEqual(ctx.exception.code, "telemetry_storage_unavailable")

    def test_unset_path_keeps_data_in_memory_only(self):
        os.environ.pop("DEVICEFABRIC_TELEMETRY_PATH", None)
        service, device, session = self.make_service_with_device()
        self.submit(service, session)
        self.assertEqual(len(self.query(service)["points"]), 1)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
