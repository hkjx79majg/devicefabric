import unittest

from devicefabric.service import Service, ServiceError

SHA_A = "a" * 64
SHA_B = "b" * 64


class FirmwareServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.credentials = {}
        for device_id in ("dev-a", "dev-b", "dev-c"):
            device = self.service.register_device(
                {"device_id": device_id, "display_name": device_id}
            )
            self.credentials[device_id] = device["credential"]

    def create_release(self, release_id="rel-1", version="1.0.0", sha256=SHA_A):
        return self.service.create_firmware_release({
            "release_id": release_id,
            "version": version,
            "download_url": f"https://fw.example.com/{release_id}.bin",
            "sha256": sha256,
        })

    def create_group(self, group_id="grp-1", device_ids=("dev-a", "dev-b")):
        return self.service.create_group(
            {"group_id": group_id, "device_ids": list(device_ids)}
        )

    def create_rollout(self, group_id="grp-1", release_id="rel-1", expected=None):
        payload = {"release_id": release_id}
        if expected is not None:
            payload["expected_group_version"] = expected
        return self.service.create_firmware_rollout(group_id, payload)

    def create_session(self, device_id):
        return self.service.create_session({
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": f"cli-{device_id}",
            "keepalive_seconds": 30,
        })

    def poll(self, session):
        return self.service.poll_firmware_update(
            session["session_id"], {"session_token": session["session_token"]}
        )

    def ack(self, session, update_id, status="installed"):
        return self.service.ack_firmware_update(
            session["session_id"], update_id,
            {"session_token": session["session_token"], "status": status},
        )

    # ------------------------------------------------------------------
    # 发布登记
    # ------------------------------------------------------------------

    def test_create_release_returns_record(self) -> None:
        release = self.create_release()
        self.assertEqual(release["release_id"], "rel-1")
        self.assertEqual(release["version"], "1.0.0")
        self.assertEqual(release["download_url"],
                         "https://fw.example.com/rel-1.bin")
        self.assertEqual(release["sha256"], SHA_A)
        self.assertIn("created_at", release)

    def test_create_duplicate_release_conflicts_even_if_identical(self) -> None:
        self.create_release()
        with self.assertRaises(ServiceError) as ctx:
            self.create_release()
        self.assertEqual(ctx.exception.code, "firmware_release_already_exists")
        self.assertEqual(ctx.exception.status, 409)

    def test_create_release_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"release_id": "rel-1", "version": "1.0.0",
             "download_url": "https://x/y"},
            {"release_id": "rel-1", "version": "1.0.0", "sha256": SHA_A},
            {"release_id": "rel-1", "download_url": "https://x/y",
             "sha256": SHA_A},
            {"version": "1.0.0", "download_url": "https://x/y",
             "sha256": SHA_A},
            {"release_id": "rel-1", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": SHA_A, "extra": 1},
            {"release_id": "bad id", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": SHA_A},
            {"release_id": "rel-1", "version": "",
             "download_url": "https://x/y", "sha256": SHA_A},
            {"release_id": "rel-1", "version": 7,
             "download_url": "https://x/y", "sha256": SHA_A},
            {"release_id": "rel-1", "version": "1.0.0",
             "download_url": "", "sha256": SHA_A},
            {"release_id": "rel-1", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": "abc"},
            {"release_id": "rel-1", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": "g" * 64},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ServiceError) as ctx:
                self.service.create_firmware_release(payload)
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._firmware_releases, {})

    # ------------------------------------------------------------------
    # 组下发
    # ------------------------------------------------------------------

    def test_rollout_freezes_version_and_member_order(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-c", "dev-a"))
        result = self.create_rollout(expected=1)
        self.assertEqual(result["group_version"], 1)
        self.assertEqual(result["release_id"], "rel-1")
        self.assertEqual(
            [u["device_id"] for u in result["updates"]], ["dev-c", "dev-a"]
        )
        update_ids = [u["update_id"] for u in result["updates"]]
        self.assertEqual(len(set(update_ids)), 2)
        # 组后续变化不影响已受理批次。
        self.service.replace_group_members("grp-1", {"device_ids": ["dev-b"]})
        view = self.service.get_firmware_rollout("grp-1", result["rollout_id"])
        self.assertEqual(view["group_version"], 1)
        self.assertEqual(
            [u["device_id"] for u in view["updates"]], ["dev-c", "dev-a"]
        )
        self.assertEqual(
            view["status_counts"],
            {"queued": 2, "delivered": 0, "installed": 0,
             "failed": 0, "cancelled": 0},
        )

    def test_rollout_empty_group_succeeds(self) -> None:
        self.create_release()
        self.create_group(device_ids=())
        result = self.create_rollout()
        self.assertEqual(result["updates"], [])
        view = self.service.get_firmware_rollout("grp-1", result["rollout_id"])
        self.assertEqual(view["updates"], [])
        self.assertEqual(
            view["status_counts"],
            {"queued": 0, "delivered": 0, "installed": 0,
             "failed": 0, "cancelled": 0},
        )

    def test_rollout_unknown_group_is_not_found(self) -> None:
        self.create_release()
        with self.assertRaises(ServiceError) as ctx:
            self.create_rollout(group_id="ghost")
        self.assertEqual(ctx.exception.code, "group_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_rollout_unknown_release_is_not_found(self) -> None:
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.create_rollout(release_id="ghost")
        self.assertEqual(ctx.exception.code, "firmware_release_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_rollout_version_conflict(self) -> None:
        self.create_release()
        self.create_group()
        with self.assertRaises(ServiceError) as ctx:
            self.create_rollout(expected=99)
        self.assertEqual(ctx.exception.code, "group_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service._rollouts, {})

    def test_rollout_with_revoked_member_rejected(self) -> None:
        self.create_release()
        self.create_group()
        self.service.revoke_device("dev-b")
        with self.assertRaises(ServiceError) as ctx:
            self.create_rollout()
        self.assertEqual(ctx.exception.code, "group_contains_revoked_device")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.service._rollouts, {})
        self.assertEqual(self.service._firmware_updates, {})

    def test_rollout_invalid_payloads(self) -> None:
        self.create_release()
        self.create_group()
        bad_payloads = [
            "not-an-object",
            {},
            {"release_id": "rel-1", "extra": 1},
            {"release_id": 5},
            {"release_id": "rel-1", "expected_group_version": -1},
            {"release_id": "rel-1", "expected_group_version": True},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ServiceError) as ctx:
                self.service.create_firmware_rollout("grp-1", payload)
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service._rollouts, {})

    def test_get_unknown_rollout_is_not_found(self) -> None:
        self.create_release()
        self.create_group()
        result = self.create_rollout()
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_firmware_rollout("grp-1", "ghost")
        self.assertEqual(ctx.exception.code, "rollout_not_found")
        self.assertEqual(ctx.exception.status, 404)
        # 其他组的批次对本组不可见。
        self.create_group(group_id="grp-2", device_ids=("dev-c",))
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_firmware_rollout("grp-2", result["rollout_id"])
        self.assertEqual(ctx.exception.code, "rollout_not_found")

    # ------------------------------------------------------------------
    # 设备领取
    # ------------------------------------------------------------------

    def test_poll_without_updates_returns_null(self) -> None:
        session = self.create_session("dev-a")
        self.assertEqual(self.poll(session), {"update": None})

    def test_poll_delivers_earliest_then_dup(self) -> None:
        self.create_release()
        self.create_release(release_id="rel-2", version="2.0.0", sha256=SHA_B)
        self.create_group(device_ids=("dev-a",))
        first = self.create_rollout()
        second = self.create_rollout(release_id="rel-2")
        session = self.create_session("dev-a")
        # 首次领取最早更新。
        got = self.poll(session)["update"]
        self.assertEqual(got["update_id"], first["updates"][0]["update_id"])
        self.assertEqual(got["status"], "delivered")
        self.assertFalse(got["dup"])
        self.assertEqual(got["version"], "1.0.0")
        self.assertEqual(got["sha256"], SHA_A)
        # 同会话重领：update_id 不变，dup 为 true。
        again = self.poll(session)["update"]
        self.assertEqual(again["update_id"], got["update_id"])
        self.assertTrue(again["dup"])
        # 确认最早更新后才能领取下一批次的更新。
        self.ack(session, got["update_id"])
        nxt = self.poll(session)["update"]
        self.assertEqual(nxt["update_id"], second["updates"][0]["update_id"])
        self.assertFalse(nxt["dup"])

    def test_poll_uses_session_auth_rules(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a",))
        self.create_rollout()
        session = self.create_session("dev-a")
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_firmware_update("ghost", {"session_token": "x"})
        self.assertEqual(ctx.exception.code, "session_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_firmware_update(
                session["session_id"], {"session_token": "wrong"}
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        # 会话被同设备同 client_id 重连取代后，旧会话领取报 409。
        self.create_session("dev-a")
        with self.assertRaises(ServiceError) as ctx:
            self.poll(session)
        self.assertEqual(ctx.exception.code, "session_not_online")

    def test_poll_invalid_payloads(self) -> None:
        session = self.create_session("dev-a")
        for payload in ("x", {}, {"session_token": 1},
                        {"session_token": session["session_token"], "x": 1}):
            with self.assertRaises(ServiceError) as ctx:
                self.service.poll_firmware_update(session["session_id"], payload)
            self.assertEqual(ctx.exception.code, "invalid_request")

    # ------------------------------------------------------------------
    # 设备确认
    # ------------------------------------------------------------------

    def test_ack_installed_updates_current_firmware_and_rollback(self) -> None:
        self.create_release()
        self.create_release(release_id="rel-2", version="2.0.0", sha256=SHA_B)
        self.create_group(device_ids=("dev-a",))
        r1 = self.create_rollout()
        session = self.create_session("dev-a")
        u1 = self.poll(session)["update"]
        done = self.ack(session, u1["update_id"])
        self.assertEqual(done["status"], "installed")
        self.assertIsNotNone(done["completed_at"])
        device = self.service._devices["dev-a"]
        self.assertEqual(device["firmware_release_id"], "rel-1")
        self.assertEqual(device["firmware_version"], "1.0.0")
        # 安装旧发布即回滚：再下发 rel-1 之后安装的 2.0.0 可被旧版覆盖。
        r2 = self.create_rollout(release_id="rel-2")
        u2 = self.poll(session)["update"]
        self.assertEqual(u2["update_id"], r2["updates"][0]["update_id"])
        self.ack(session, u2["update_id"])
        self.assertEqual(device["firmware_version"], "2.0.0")
        r3 = self.create_rollout()
        u3 = self.poll(session)["update"]
        self.assertEqual(u3["update_id"], r3["updates"][0]["update_id"])
        self.ack(session, u3["update_id"])
        self.assertEqual(device["firmware_release_id"], "rel-1")
        self.assertEqual(device["firmware_version"], "1.0.0")

    def test_ack_failed_does_not_change_firmware(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a",))
        self.create_rollout()
        session = self.create_session("dev-a")
        update = self.poll(session)["update"]
        done = self.ack(session, update["update_id"], status="failed")
        self.assertEqual(done["status"], "failed")
        self.assertIsNone(self.service._devices["dev-a"]["firmware_release_id"])

    def test_ack_same_status_idempotent(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a",))
        self.create_rollout()
        session = self.create_session("dev-a")
        update = self.poll(session)["update"]
        first = self.ack(session, update["update_id"])
        again = self.ack(session, update["update_id"])
        self.assertEqual(first, again)

    def test_ack_conflicting_status_rejected(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a",))
        self.create_rollout()
        session = self.create_session("dev-a")
        update = self.poll(session)["update"]
        self.ack(session, update["update_id"], status="failed")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, update["update_id"], status="installed")
        self.assertEqual(ctx.exception.code, "firmware_update_already_completed")
        self.assertEqual(ctx.exception.status, 409)

    def test_ack_not_delivered_rejected(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a", "dev-b"))
        rollout = self.create_rollout()
        update_id = rollout["updates"][0]["update_id"]
        session_a = self.create_session("dev-a")
        # 尚未领取即确认。
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_a, update_id)
        self.assertEqual(ctx.exception.code, "firmware_update_not_delivered")
        self.assertEqual(ctx.exception.status, 409)
        # 其他会话的已投递更新对本会话也是未领取。
        session_b = self.create_session("dev-b")
        other_id = rollout["updates"][1]["update_id"]
        self.poll(session_b)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_b, update_id)
        self.assertEqual(ctx.exception.code, "firmware_update_not_found")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_a, other_id)
        self.assertEqual(ctx.exception.code, "firmware_update_not_found")

    def test_ack_unknown_update_is_not_found(self) -> None:
        session = self.create_session("dev-a")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, "ghost")
        self.assertEqual(ctx.exception.code, "firmware_update_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_ack_invalid_status_rejected_without_state_change(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a",))
        self.create_rollout()
        session = self.create_session("dev-a")
        update = self.poll(session)["update"]
        for status in ("succeeded", "done", "", 1, True, None):
            with self.assertRaises(ServiceError) as ctx:
                self.ack(session, update["update_id"], status=status)
            self.assertEqual(ctx.exception.code, "invalid_request")
            self.assertEqual(ctx.exception.status, 400)
        current = self.service._firmware_updates[update["update_id"]]
        self.assertEqual(current["status"], "delivered")

    def test_ack_invalid_payloads(self) -> None:
        session = self.create_session("dev-a")
        for payload in ("x", {}, {"session_token": "t"},
                        {"status": "installed"},
                        {"session_token": "t", "status": "installed", "x": 1}):
            with self.assertRaises(ServiceError) as ctx:
                self.service.ack_firmware_update(
                    session["session_id"], "u1", payload
                )
            self.assertEqual(ctx.exception.code, "invalid_request")

    # ------------------------------------------------------------------
    # 吊销与状态计数
    # ------------------------------------------------------------------

    def test_revoke_cancels_non_terminal_updates(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a", "dev-b"))
        rollout = self.create_rollout()
        session_a = self.create_session("dev-a")
        session_b = self.create_session("dev-b")
        delivered = self.poll(session_a)["update"]
        installed = self.poll(session_b)["update"]
        self.ack(session_b, installed["update_id"])
        # 再下发一批，dev-a 的更新尚未领取。
        second = self.create_rollout()
        self.service.revoke_device("dev-a")
        view = self.service.get_firmware_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(
            view["status_counts"],
            {"queued": 0, "delivered": 0, "installed": 1,
             "failed": 0, "cancelled": 1},
        )
        view2 = self.service.get_firmware_rollout("grp-1", second["rollout_id"])
        self.assertEqual(
            view2["status_counts"],
            {"queued": 1, "delivered": 0, "installed": 0,
             "failed": 0, "cancelled": 1},
        )
        # 已 cancelled 的更新保持终态；吊销设备的会话已关闭，无法确认。
        cancelled_id = second["updates"][0]["update_id"]
        self.assertEqual(
            self.service._firmware_updates[cancelled_id]["status"], "cancelled"
        )
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session_a, delivered["update_id"])
        self.assertEqual(ctx.exception.code, "session_not_online")

    def test_status_counts_cover_five_states(self) -> None:
        self.create_release()
        self.create_group(device_ids=("dev-a", "dev-b", "dev-c"))
        rollout = self.create_rollout()
        session_a = self.create_session("dev-a")
        session_b = self.create_session("dev-b")
        delivered = self.poll(session_a)["update"]
        failed = self.poll(session_b)["update"]
        self.ack(session_b, failed["update_id"], status="failed")
        self.service.revoke_device("dev-c")
        view = self.service.get_firmware_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(
            view["status_counts"],
            {"queued": 0, "delivered": 1, "installed": 0,
             "failed": 1, "cancelled": 1},
        )
        self.assertEqual(delivered["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
