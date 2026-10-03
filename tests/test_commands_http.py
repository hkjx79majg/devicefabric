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
        status, self.session = self.call("POST", "/v1/device-sessions", {
            "device_id": "sensor-1",
            "credential": self.device["credential"],
            "client_id": "cli-1",
            "keepalive_seconds": 30,
        })
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

    def create_command(self, name="reboot", payload=None, ttl=60, device_id="sensor-1"):
        if payload is None:
            payload = {"delay": 3}
        return self.call("POST", f"/v1/devices/{device_id}/commands", {
            "command_name": name,
            "payload": payload,
            "ttl_seconds": ttl,
        })

    def poll(self, max_commands=10):
        return self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/commands/poll",
            {
                "session_token": self.session["session_token"],
                "max_commands": max_commands,
            },
        )

    def ack(self, command_id, status="succeeded", result=None):
        if result is None:
            result = {"exit_code": 0}
        return self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/commands/{command_id}/ack",
            {
                "session_token": self.session["session_token"],
                "status": status,
                "result": result,
            },
        )

    def test_create_poll_ack_get_flow(self) -> None:
        status, command = self.create_command()
        self.assertEqual(status, 202)
        self.assertTrue(command["command_id"])
        self.assertEqual(command["status"], "queued")
        self.assertEqual(command["device_id"], "sensor-1")

        status, polled = self.poll()
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["commands"]), 1)
        item = polled["commands"][0]
        self.assertEqual(item["command_id"], command["command_id"])
        self.assertEqual(item["status"], "delivered")
        self.assertFalse(item["dup"])
        self.assertEqual(item["delivery_count"], 1)

        status, completed = self.ack(command["command_id"])
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"], {"exit_code": 0})
        self.assertIsNotNone(completed["completed_at"])

        status, snapshot = self.call(
            "GET", f"/v1/devices/sensor-1/commands/{command['command_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["command_name"], "reboot")
        self.assertEqual(snapshot["payload"], {"delay": 3})
        self.assertEqual(snapshot["ttl_seconds"], 60)

    def test_create_command_errors(self) -> None:
        status, body = self.create_command(device_id="ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": "bad name", "payload": None, "ttl_seconds": 60,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": "reboot", "payload": None, "ttl_seconds": 4,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call("POST", "/v1/devices/sensor-1/commands", {
            "command_name": "reboot", "payload": None,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        self.call("POST", "/v1/devices/sensor-1/revoke")
        status, body = self.create_command()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_revoked")

    def test_poll_and_ack_session_auth_errors(self) -> None:
        status, command = self.create_command()
        self.assertEqual(status, 202)

        status, body = self.call(
            "POST", "/v1/device-sessions/ghost/commands/poll",
            {"session_token": "x", "max_commands": 1},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "session_not_found")

        status, body = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/commands/poll",
            {"session_token": "wrong", "max_commands": 1},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_session_token")

        status, body = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}/commands/poll",
            {"session_token": self.session["session_token"], "max_commands": 0},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.ack(command["command_id"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "command_not_delivered")

        status, body = self.ack("no-such-command")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "command_not_found")

        status, body = self.call(
            "POST",
            f"/v1/device-sessions/{self.session['session_id']}"
            f"/commands/{command['command_id']}/ack",
            {"session_token": self.session["session_token"], "status": "done",
             "result": None},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_get_command_errors(self) -> None:
        status, command = self.create_command()
        self.assertEqual(status, 202)
        status, body = self.call(
            "GET", f"/v1/devices/ghost/commands/{command['command_id']}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        status, body = self.call("GET", "/v1/devices/sensor-1/commands/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "command_not_found")

    def test_ack_idempotent_and_conflict(self) -> None:
        status, command = self.create_command()
        self.assertEqual(status, 202)
        status, _ = self.poll()
        self.assertEqual(status, 200)
        status, first = self.ack(command["command_id"])
        self.assertEqual(status, 200)
        status, again = self.ack(command["command_id"])
        self.assertEqual(status, 200)
        self.assertEqual(again, first)
        status, body = self.ack(command["command_id"], result={"exit_code": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "command_already_completed")


if __name__ == "__main__":
    unittest.main()
