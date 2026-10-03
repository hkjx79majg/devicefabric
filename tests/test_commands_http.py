import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class CommandHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的 Service，保证进程内状态互不影响。
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, self.device = self.call(
            "POST", "/v1/devices", {"device_id": "sensor-1", "display_name": "传感器"}
        )
        self.assertEqual(status, 201)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None, raw_body: bytes | None = None):
        data = raw_body
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def connect(self, client_id="cli-1", keepalive=30):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })
        self.assertEqual(status, 201)
        return session

    def create_command(self, name="reboot", payload=None, ttl=60):
        return self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": name,
            "payload": payload,
            "ttl_seconds": ttl,
        })

    def poll(self, session, max_commands=10):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/commands/poll",
            {"session_token": session["session_token"], "max_commands": max_commands},
        )

    def ack(self, session, command_id, status="succeeded", result=None):
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}/commands/{command_id}/ack",
            {"session_token": session["session_token"], "status": status, "result": result},
        )

    def test_create_poll_ack_flow(self) -> None:
        status, created = self.create_command(payload={"delay": 3})
        self.assertEqual(status, 202)
        self.assertEqual(created["status"], "queued")
        self.assertEqual(created["delivery_count"], 0)
        command_id = created["command_id"]

        session = self.connect()
        status, polled = self.poll(session)
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["commands"]), 1)
        entry = polled["commands"][0]
        self.assertEqual(entry["command_id"], command_id)
        self.assertEqual(entry["command_name"], "reboot")
        self.assertEqual(entry["payload"], {"delay": 3})
        self.assertFalse(entry["dup"])
        self.assertEqual(entry["delivery_count"], 1)

        status, acked = self.ack(session, command_id, result={"code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(acked["status"], "succeeded")
        self.assertEqual(acked["result"], {"code": 0})
        self.assertIsNotNone(acked["completed_at"])

        status, snapshot = self.call(
            "GET", f"/v1/devices/sensor-1/commands/{command_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["delivery_count"], 1)

    def test_create_command_errors(self) -> None:
        status, body = self.call("POST", "/v1/devices/no-such/commands", {
            "command_name": "reboot", "payload": None, "ttl_seconds": 30,
        })
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": "reboot", "payload": None, "ttl_seconds": 4,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": "reboot", "ttl_seconds": 30,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        self.call("POST", "/v1/devices/sensor-1/revoke")
        status, body = self.create_command()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_revoked")

    def test_get_command_errors(self) -> None:
        status, body = self.call("GET", "/v1/devices/no-such/commands/abc")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        status, body = self.call("GET", "/v1/devices/sensor-1/commands/abc")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "command_not_found")

    def test_poll_session_auth_errors(self) -> None:
        session = self.connect()
        status, body = self.call(
            "POST", "/v1/device-sessions/no-such/commands/poll",
            {"session_token": session["session_token"], "max_commands": 1},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "session_not_found")
        status, body = self.poll(session, max_commands=0)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        status, body = self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/commands/poll",
            {"session_token": "wrong", "max_commands": 1},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_session_token")

    def test_ack_conflict_and_not_delivered(self) -> None:
        status, created = self.create_command()
        command_id = created["command_id"]
        session = self.connect()

        # 未投递不能确认。
        status, body = self.ack(session, command_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "command_not_delivered")

        self.poll(session)
        status, acked = self.ack(session, command_id, result={"ok": True})
        self.assertEqual(status, 200)
        # 相同确认幂等。
        status, replay = self.ack(session, command_id, result={"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(replay, acked)
        # 内容冲突。
        status, body = self.ack(session, command_id, status="failed", result={"ok": True})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "command_already_completed")

    def test_ack_unknown_command(self) -> None:
        session = self.connect()
        status, body = self.ack(session, "no-such-command")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "command_not_found")

    def test_revoke_cancels_open_commands(self) -> None:
        status, created = self.create_command()
        command_id = created["command_id"]
        self.call("POST", "/v1/devices/sensor-1/revoke")
        status, snapshot = self.call(
            "GET", f"/v1/devices/sensor-1/commands/{command_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "cancelled")

    def test_expired_command_rejects_ack(self) -> None:
        status, created = self.create_command(ttl=5)
        command_id = created["command_id"]
        # 直接把进程内命令的有效期拨到过去，模拟到期。
        from datetime import timedelta

        from devicefabric.service import _utc_now
        handler_service = self.server.RequestHandlerClass.service
        handler_service._commands[command_id]["expires_at"] = (
            _utc_now() - timedelta(seconds=1)
        )
        session = self.connect()
        status, polled = self.poll(session)
        self.assertEqual(status, 200)
        self.assertEqual(polled["commands"], [])
        status, body = self.ack(session, command_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "command_expired")
