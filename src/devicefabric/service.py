"""Core service surface for DeviceFabric.

Health reporting remains the frozen baseline. This module also owns the
in-process device registry and credential lifecycle; state lives only for
the lifetime of the process and is cleared on restart.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import __version__

_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DISPLAY_NAME_MIN = 1
_DISPLAY_NAME_MAX = 128
_CREDENTIAL_BYTES = 32


class ServiceError(Exception):
    """An error with a fixed HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class _Device:
    device_id: str
    display_name: str
    created_at: str
    credential: str
    credential_version: int = 1
    active: bool = True


def _utc_now() -> str:
    """Return the current UTC time as an RFC 3339 timestamp with a Z suffix."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return stamp.removesuffix("+00:00") + "Z"


def _invalid_request(message: str) -> ServiceError:
    return ServiceError(400, "invalid_request", message)


def _validate_registration(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise _invalid_request("request body must be a JSON object")
    required = {"device_id", "display_name"}
    keys = set(payload)
    if keys != required:
        missing = sorted(required - keys)
        unknown = sorted(keys - required)
        if missing:
            raise _invalid_request(f"missing required field(s): {', '.join(missing)}")
        raise _invalid_request(f"unknown field(s): {', '.join(unknown)}")
    device_id = payload["device_id"]
    display_name = payload["display_name"]
    if not isinstance(device_id, str) or _DEVICE_ID_RE.fullmatch(device_id) is None:
        raise _invalid_request(
            "device_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    if (
        not isinstance(display_name, str)
        or not _DISPLAY_NAME_MIN <= len(display_name) <= _DISPLAY_NAME_MAX
    ):
        raise _invalid_request("display_name must be 1-128 Unicode characters")
    return device_id, display_name


def _validate_credentials(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise _invalid_request("request body must be a JSON object")
    required = {"device_id", "credential"}
    keys = set(payload)
    if keys != required:
        missing = sorted(required - keys)
        unknown = sorted(keys - required)
        if missing:
            raise _invalid_request(f"missing required field(s): {', '.join(missing)}")
        raise _invalid_request(f"unknown field(s): {', '.join(unknown)}")
    device_id = payload["device_id"]
    credential = payload["credential"]
    if not isinstance(device_id, str) or not device_id:
        raise _invalid_request("device_id must be a non-empty string")
    if not isinstance(credential, str):
        raise _invalid_request("credential must be a string")
    return device_id, credential


class Service:
    """In-process device registry. All state is lost on restart."""

    name = "devicefabric"
    version = __version__

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, _Device] = {}
        self._credentials: set[str] = set()

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def register_device(self, payload: object) -> dict:
        device_id, display_name = _validate_registration(payload)
        with self._lock:
            if device_id in self._devices:
                raise ServiceError(
                    409,
                    "device_already_exists",
                    f"device {device_id!r} is already registered",
                )
            credential = self._issue_credential()
            device = _Device(
                device_id=device_id,
                display_name=display_name,
                created_at=_utc_now(),
                credential=credential,
            )
            self._devices[device_id] = device
            return self._public_view(device, credential=credential)

    def get_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._require_device(device_id)
            return self._public_view(device)

    def authenticate(self, payload: object) -> dict[str, bool]:
        device_id, credential = _validate_credentials(payload)
        with self._lock:
            device = self._devices.get(device_id)
            if (
                device is None
                or not device.active
                or not hmac.compare_digest(credential, device.credential)
            ):
                raise ServiceError(
                    401,
                    "invalid_credential",
                    "credential is invalid, rotated, or the device is revoked",
                )
        return {"authenticated": True}

    def rotate_credential(self, device_id: str) -> dict:
        with self._lock:
            device = self._require_device(device_id)
            if not device.active:
                raise ServiceError(
                    409, "device_revoked", f"device {device_id!r} is revoked"
                )
            credential = self._issue_credential()
            device.credential = credential
            device.credential_version += 1
            return {
                "credential": credential,
                "credential_version": device.credential_version,
            }

    def revoke_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._require_device(device_id)
            device.active = False
            return self._public_view(device)

    def _require_device(self, device_id: str) -> _Device:
        device = self._devices.get(device_id)
        if device is None:
            raise ServiceError(
                404, "device_not_found", f"device {device_id!r} does not exist"
            )
        return device

    def _issue_credential(self) -> str:
        # Caller holds the lock; guarantee uniqueness within this process.
        while True:
            credential = secrets.token_urlsafe(_CREDENTIAL_BYTES)
            if credential and credential not in self._credentials:
                self._credentials.add(credential)
                return credential

    @staticmethod
    def _public_view(device: _Device, credential: str | None = None) -> dict:
        view = {
            "device_id": device.device_id,
            "display_name": device.display_name,
            "active": device.active,
            "created_at": device.created_at,
            "credential_version": device.credential_version,
        }
        if credential is not None:
            view["credential"] = credential
        return view
