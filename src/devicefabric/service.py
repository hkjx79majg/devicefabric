"""Core service surface for DeviceFabric.

除健康检查外，本模块实现设备注册与身份凭据生命周期（注册、查询、
认证、轮换与吊销），以及进程内连接会话与心跳保活。所有数据仅保存
在当前进程内，进程退出即清空。
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import __version__

DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
DEVICE_ID_MIN = 1
DEVICE_ID_MAX = 64
DISPLAY_NAME_MIN = 1
DISPLAY_NAME_MAX = 128
KEEPALIVE_MIN = 5
KEEPALIVE_MAX = 3600

SESSION_ONLINE = "online"
SESSION_CLOSED = "closed"
SESSION_EXPIRED = "expired"


class ServiceError(Exception):
    """业务错误，携带稳定的 error.code、message 与 HTTP 状态码。"""

    code = "invalid_request"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339_utc(dt: datetime) -> str:
    """以 RFC 3339 格式输出 UTC 时间，规范时区后缀写作 ``Z``。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_device_id(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("device_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "device_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_display_name(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("display_name must be a string")
    length = len(value)
    if not DISPLAY_NAME_MIN <= length <= DISPLAY_NAME_MAX:
        raise ServiceError("display_name must be 1-128 Unicode characters")
    return value


def _validate_client_id(value: object) -> str:
    # client_id 沿用 device_id 的校验规则。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("client_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "client_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_keepalive(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("keepalive_seconds must be an integer")
    if not KEEPALIVE_MIN <= value <= KEEPALIVE_MAX:
        raise ServiceError(
            f"keepalive_seconds must be between {KEEPALIVE_MIN} and {KEEPALIVE_MAX}"
        )
    return value


class Service:
    """进程内设备注册与凭据生命周期服务。"""

    name = "devicefabric"
    version = __version__

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        # 记录本进程签发过的全部凭据，保证凭据在进程内绝不重复。
        self._issued_credentials: set[str] = set()
        self._sessions: dict[str, dict] = {}
        # 仅索引在线会话：(device_id, client_id) -> session_id。
        self._online_session_keys: dict[tuple[str, str], str] = {}
        # 记录本进程签发过的全部会话令牌，保证令牌在进程内绝不重复。
        self._issued_session_tokens: set[str] = set()

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    @staticmethod
    def _public_device(device: dict) -> dict:
        return {
            "device_id": device["device_id"],
            "display_name": device["display_name"],
            "active": device["active"],
            "created_at": device["created_at"],
            "credential_version": device["credential_version"],
        }

    def _mint_credential_locked(self) -> str:
        credential = secrets.token_urlsafe(32)
        while not credential or credential in self._issued_credentials:
            credential = secrets.token_urlsafe(32)
        self._issued_credentials.add(credential)
        return credential

    def register_device(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "display_name"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "device_id" not in payload:
            raise ServiceError("missing required field: device_id")
        if "display_name" not in payload:
            raise ServiceError("missing required field: display_name")
        device_id = _validate_device_id(payload["device_id"])
        display_name = _validate_display_name(payload["display_name"])

        with self._lock:
            if device_id in self._devices:
                raise ServiceError(
                    f"device {device_id!r} already exists",
                    code="device_already_exists",
                    status=409,
                )
            credential = self._mint_credential_locked()
            device = {
                "device_id": device_id,
                "display_name": display_name,
                "active": True,
                "created_at": _rfc3339_utc(_utc_now()),
                "credential_version": 1,
                "credential": credential,
            }
            self._devices[device_id] = device
            result = self._public_device(device)
            result["credential"] = credential
            return result

    def get_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            return self._public_device(device)

    def authenticate(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "credential"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "device_id" not in payload:
            raise ServiceError("missing required field: device_id")
        if "credential" not in payload:
            raise ServiceError("missing required field: credential")
        device_id = payload["device_id"]
        credential = payload["credential"]
        if not isinstance(device_id, str) or not isinstance(credential, str):
            raise ServiceError("device_id and credential must be strings")

        with self._lock:
            device = self._devices.get(device_id)
            valid = (
                device is not None
                and device["active"]
                and bool(credential)
                and hmac.compare_digest(
                    device["credential"].encode("utf-8"), credential.encode("utf-8")
                )
            )
            if not valid:
                raise ServiceError(
                    "invalid credential", code="invalid_credential", status=401
                )
            return {"authenticated": True}

    def rotate_credential(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            if not device["active"]:
                raise ServiceError(
                    f"device {device_id!r} is revoked",
                    code="device_revoked",
                    status=409,
                )
            credential = self._mint_credential_locked()
            device["credential"] = credential
            device["credential_version"] += 1
            result = self._public_device(device)
            result["credential"] = credential
            return result

    def revoke_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            # 吊销是幂等的：重复调用保持状态与版本不变。
            device["active"] = False
            # 吊销立即关闭该设备的全部在线会话。
            for key, session_id in list(self._online_session_keys.items()):
                if key[0] == device_id:
                    self._close_session_locked(self._sessions[session_id], "device_revoked")
            return self._public_device(device)

    # ------------------------------------------------------------------
    # 连接会话与心跳保活
    # ------------------------------------------------------------------

    @staticmethod
    def _public_session(session: dict) -> dict:
        """会话快照，绝不包含 session_token。"""
        return {
            "session_id": session["session_id"],
            "device_id": session["device_id"],
            "client_id": session["client_id"],
            "connected_at": _rfc3339_utc(session["connected_at"]),
            "last_seen_at": _rfc3339_utc(session["last_seen_at"]),
            "expires_at": _rfc3339_utc(session["expires_at"]),
            "online": session["state"] == SESSION_ONLINE,
            "state": session["state"],
            "reason": session["reason"],
        }

    def _mint_session_id_locked(self) -> str:
        session_id = secrets.token_urlsafe(18)
        while session_id in self._sessions:
            session_id = secrets.token_urlsafe(18)
        return session_id

    def _mint_session_token_locked(self) -> str:
        token = secrets.token_urlsafe(32)
        while not token or token in self._issued_session_tokens:
            token = secrets.token_urlsafe(32)
        self._issued_session_tokens.add(token)
        return token

    def _expire_if_timed_out_locked(self, session: dict, now: datetime) -> bool:
        """在线会话超过 expires_at 即转为 expired，不可恢复。"""
        if session["state"] == SESSION_ONLINE and now > session["expires_at"]:
            session["state"] = SESSION_EXPIRED
            session["reason"] = "keepalive_timeout"
            self._online_session_keys.pop(
                (session["device_id"], session["client_id"]), None
            )
            return True
        return False

    def _close_session_locked(self, session: dict, reason: str) -> None:
        session["state"] = SESSION_CLOSED
        session["reason"] = reason
        self._online_session_keys.pop(
            (session["device_id"], session["client_id"]), None
        )

    def create_session(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "credential", "client_id", "keepalive_seconds"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for field in ("device_id", "credential", "client_id", "keepalive_seconds"):
            if field not in payload:
                raise ServiceError(f"missing required field: {field}")
        device_id = _validate_device_id(payload["device_id"])
        client_id = _validate_client_id(payload["client_id"])
        credential = payload["credential"]
        if not isinstance(credential, str):
            raise ServiceError("credential must be a string")
        keepalive = _validate_keepalive(payload["keepalive_seconds"])

        with self._lock:
            device = self._devices.get(device_id)
            valid = (
                device is not None
                and device["active"]
                and bool(credential)
                and hmac.compare_digest(
                    device["credential"].encode("utf-8"), credential.encode("utf-8")
                )
            )
            if not valid:
                raise ServiceError(
                    "invalid credential", code="invalid_credential", status=401
                )
            now = _utc_now()
            key = (device_id, client_id)
            previous_id = self._online_session_keys.get(key)
            if previous_id is not None:
                previous = self._sessions[previous_id]
                # 同设备同 client_id 重连：旧会话若已超时按超时过期，
                # 否则标记为被新会话取代。
                if not self._expire_if_timed_out_locked(previous, now):
                    self._close_session_locked(previous, "replaced")
            session = {
                "session_id": self._mint_session_id_locked(),
                "token": self._mint_session_token_locked(),
                "device_id": device_id,
                "client_id": client_id,
                "keepalive_seconds": keepalive,
                "connected_at": now,
                "last_seen_at": now,
                "expires_at": now + timedelta(seconds=keepalive),
                "state": SESSION_ONLINE,
                "reason": None,
            }
            self._sessions[session["session_id"]] = session
            self._online_session_keys[key] = session["session_id"]
            result = self._public_session(session)
            # session_token 仅在创建响应中返回一次。
            result["session_token"] = session["token"]
            return result

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ServiceError(
                    f"session {session_id!r} not found",
                    code="session_not_found",
                    status=404,
                )
            self._expire_if_timed_out_locked(session, _utc_now())
            return self._public_session(session)

    def heartbeat_session(self, session_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"session_token"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "session_token" not in payload:
            raise ServiceError("missing required field: session_token")
        token = payload["session_token"]
        if not isinstance(token, str):
            raise ServiceError("session_token must be a string")

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ServiceError(
                    f"session {session_id!r} not found",
                    code="session_not_found",
                    status=404,
                )
            now = _utc_now()
            # 心跳也会触发超时判定；已过期会话不能恢复。
            self._expire_if_timed_out_locked(session, now)
            if not hmac.compare_digest(
                session["token"].encode("utf-8"), token.encode("utf-8")
            ):
                raise ServiceError(
                    "invalid session token",
                    code="invalid_session_token",
                    status=401,
                )
            if session["state"] != SESSION_ONLINE:
                raise ServiceError(
                    f"session {session_id!r} is not online",
                    code="session_not_online",
                    status=409,
                )
            session["last_seen_at"] = now
            session["expires_at"] = now + timedelta(
                seconds=session["keepalive_seconds"]
            )
            return self._public_session(session)
