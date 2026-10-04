import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service

SHA_A = "a" * 64
SHA_B = "B" * 64


class FirmwareHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.credentials = {}
        for device_id in ("dev-a", "dev-b"):
            status, device = self.call(
                "POST", "/v1/devices",
                {"device_id": device_id, "display_name": device_id},
            )
            self.assertEqual(status, 201)
            self.credentials[device_id] = device["credential"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None, raw=None):
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = b"{}"
        req = urllib_request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def create_release(self, release_id="rel-1", version="1.0.0", sha256=SHA_A):
        return self.call("POST", "/v1/firmware-releases", {
            "release_id": release_id,
            "version": version,
            "download_url": f"https://fw.example.com/{release_id}.bin",
            "sha256": sha256,
        })

    def create_group(self, group_id="grp-1", device_ids=("dev-a", "dev-b")):
        return self.call("POST", "/v1/device-groups", {
            "group_id": group_id, "device_ids": list(device_ids),
        })

    def create_rollout(self, group_id="grp-1", release_id="rel-1", expected=None):
        body = {"release_id": release_id}
        if expected is not None:
            body["expected_group_version"] = expected
        return self.call(
            "POST", f"/v1/device-groups/{group_id}/firmware-rollouts", body
        )

    def get_rollout(self, group_id, rollout_id):
        return self.call(
            "GET",
            f"/v1/device-groups/{group_id}/firmware-rollouts/{rollout_id}",
        )

    def create_session(self, device_id):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": f"cli-{device_id}",
            "keepalive_seconds": 30,
        })
        self.assertEqual(status, 201)
        return session

    def poll(self, session):
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}/firmware/poll",
            {"session_token": session["session_token"]},
        )

    def ack(self, session, update_id, status="installed"):
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}"
            f"/firmware/{update_id}/ack",
            {"session_token": session["session_token"], "status": status},
        )

    def test_release_lifecycle(self) -> None:
        status, release = self.create_release()
        self.assertEqual(status, 201)
        self.assertEqual(release["release_id"], "rel-1")
        self.assertEqual(release["version"], "1.0.0")
        self.assertEqual(release["sha256"], SHA_A)
        # 重名冲突，即使内容完全相同。
        status, err = self.create_release()
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "firmware_release_already_exists")
        # 缺字段 / 多余字段 / 非法字段均为 400。
        for body in (
            {},
            {"release_id": "rel-2", "version": "1.0.0",
             "download_url": "https://x/y"},
            {"release_id": "rel-2", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": SHA_A, "extra": 1},
            {"release_id": "rel-2", "version": "1.0.0",
             "download_url": "https://x/y", "sha256": "zz"},
        ):
            status, err = self.call("POST", "/v1/firmware-releases", body)
            self.assertEqual(status, 400)
            self.assertEqual(err["error"]["code"], "invalid_request")

    def test_rollout_flow_end_to_end(self) -> None:
        self.assertEqual(self.create_release()[0], 201)
        self.assertEqual(self.create_group()[0], 201)
        status, rollout = self.create_rollout(expected=1)
        self.assertEqual(status, 202)
        self.assertEqual(rollout["group_version"], 1)
        self.assertEqual(
            [u["device_id"] for u in rollout["updates"]], ["dev-a", "dev-b"]
        )
        update_ids = {u["device_id"]: u["update_id"]
                      for u in rollout["updates"]}

        session_a = self.create_session("dev-a")
        # 无更新时（dev-b 未轮询前 dev-a 已有一条）——先看 dev-a 领取。
        status, got = self.poll(session_a)
        self.assertEqual(status, 200)
        self.assertEqual(got["update"]["update_id"], update_ids["dev-a"])
        self.assertFalse(got["update"]["dup"])
        self.assertEqual(got["update"]["download_url"],
                         "https://fw.example.com/rel-1.bin")
        # 同会话重领 dup=true。
        status, again = self.poll(session_a)
        self.assertEqual(status, 200)
        self.assertEqual(again["update"]["update_id"], update_ids["dev-a"])
        self.assertTrue(again["update"]["dup"])
        # 确认 installed。
        status, done = self.ack(session_a, update_ids["dev-a"])
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "installed")
        # 相同状态重复确认幂等。
        status, repeat = self.ack(session_a, update_ids["dev-a"])
        self.assertEqual(status, 200)
        self.assertEqual(repeat, done)
        # 状态冲突 409。
        status, err = self.ack(session_a, update_ids["dev-a"], status="failed")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"],
                         "firmware_update_already_completed")
        # dev-a 已无待领取更新。
        status, got = self.poll(session_a)
        self.assertEqual(status, 200)
        self.assertIsNone(got["update"])
        # 批次查询：五状态计数。
        status, view = self.get_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(status, 200)
        self.assertEqual(view["release_id"], "rel-1")
        self.assertEqual(
            view["status_counts"],
            {"queued": 1, "delivered": 0, "installed": 1,
             "failed": 0, "cancelled": 0},
        )

    def test_rollout_errors(self) -> None:
        self.assertEqual(self.create_release()[0], 201)
        self.assertEqual(self.create_group()[0], 201)
        status, err = self.create_rollout(group_id="ghost")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "group_not_found")
        status, err = self.create_rollout(release_id="ghost")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "firmware_release_not_found")
        status, err = self.create_rollout(expected=7)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "group_version_conflict")
        # 含吊销成员。
        self.call("POST", "/v1/devices/dev-b/revoke")
        status, err = self.create_rollout()
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "group_contains_revoked_device")
        # 未知批次。
        status, err = self.get_rollout("grp-1", "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "rollout_not_found")

    def test_empty_group_rollout(self) -> None:
        self.assertEqual(self.create_release()[0], 201)
        self.assertEqual(self.create_group(device_ids=())[0], 201)
        status, rollout = self.create_rollout()
        self.assertEqual(status, 202)
        self.assertEqual(rollout["updates"], [])
        status, view = self.get_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(status, 200)
        self.assertEqual(view["updates"], [])
        self.assertEqual(
            view["status_counts"],
            {"queued": 0, "delivered": 0, "installed": 0,
             "failed": 0, "cancelled": 0},
        )

    def test_poll_and_ack_errors(self) -> None:
        self.assertEqual(self.create_release()[0], 201)
        self.assertEqual(self.create_group(device_ids=("dev-a",))[0], 201)
        status, rollout = self.create_rollout()
        self.assertEqual(status, 202)
        update_id = rollout["updates"][0]["update_id"]
        session = self.create_session("dev-a")
        # 未领取即确认。
        status, err = self.ack(session, update_id)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "firmware_update_not_delivered")
        # 非本设备更新。
        status, err = self.ack(session, "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "firmware_update_not_found")
        # 非法 status 为 400 且状态不变。
        status, err = self.ack(session, update_id, status="succeeded")
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "invalid_request")
        status, view = self.get_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(view["status_counts"]["queued"], 1)
        # 会话令牌错误沿用既有结果。
        status, err = self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}/firmware/poll",
            {"session_token": "wrong"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(err["error"]["code"], "invalid_session_token")

    def test_revoke_cancels_pending_updates(self) -> None:
        self.assertEqual(self.create_release()[0], 201)
        self.assertEqual(self.create_group(device_ids=("dev-a",))[0], 201)
        _, rollout = self.create_rollout()
        self.call("POST", "/v1/devices/dev-a/revoke")
        status, view = self.get_rollout("grp-1", rollout["rollout_id"])
        self.assertEqual(status, 200)
        self.assertEqual(view["status_counts"]["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
