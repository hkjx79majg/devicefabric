"""多租户审计与诊断导出（独立功能模块）。

本模块提供与既有设备/连接/路由/消息入口完全解耦的审计能力：
调用方自行创建 :class:`AuditStore` 并显式写入事件，既有入口不会
自动产生审计事件。所有数据仅保存在当前进程内，进程退出即清空，
本模块不进行任何文件写入。

- 写入校验失败统一抛出 :class:`AuditValidationError`，且不产生部分记录。
- 游标损坏、跨租户使用、筛选条件变更或超出快照范围时统一抛出
  :class:`AuditCursorError`。
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "AuditCursorError",
    "AuditStore",
    "AuditValidationError",
    "create_audit_store",
]

PAGE_SIZE_MIN = 1
PAGE_SIZE_MAX = 1000
PAGE_SIZE_DEFAULT = 100

REDACTED_VALUE = "[REDACTED]"
# 诊断导出时需要遮蔽的详情键（大小写不敏感）。
REDACTED_KEYS = frozenset(
    {"password", "token", "secret", "authorization", "credential"}
)

_CURSOR_VERSION = 1
_OPTIONAL_FIELDS = ("device_id", "session_id", "correlation_id")


class AuditValidationError(Exception):
    """审计写入、查询或导出的参数校验失败。"""


class AuditCursorError(Exception):
    """审计分页游标无效、被篡改、跨租户或超出快照范围。"""


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditValidationError(f"{field} must be a non-empty string")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty_str(value, field)


def _coerce_time(value: Any, field: str) -> datetime:
    """将输入规范化为 UTC 的带时区时间；非法输入抛出校验错误。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise AuditValidationError(
                f"{field} must be a valid timezone-aware timestamp"
            ) from None
    else:
        raise AuditValidationError(
            f"{field} must be a valid timezone-aware timestamp"
        )
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise AuditValidationError(
            f"{field} must be a valid timezone-aware timestamp"
        )
    return parsed.astimezone(timezone.utc)


def _format_time(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _validate_details(details: Any) -> dict | None:
    if details is None:
        return None
    if not isinstance(details, dict):
        raise AuditValidationError("details must be a JSON-serializable object")
    try:
        # 与导出使用相同的序列化选项，确保写入通过校验的事件日后必然可导出。
        json.dumps(details, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise AuditValidationError(
            "details must be a JSON-serializable object"
        ) from None
    return copy.deepcopy(details)


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AuditValidationError("limit must be an integer between 1 and 1000")
    if limit < PAGE_SIZE_MIN or limit > PAGE_SIZE_MAX:
        raise AuditValidationError("limit must be an integer between 1 and 1000")
    return limit


def _redact(value: Any) -> Any:
    """递归遮蔽敏感键；键保留，值固定替换为 [REDACTED]。"""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in REDACTED_KEYS:
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _filters_fingerprint(filters: dict) -> str:
    canonical = json.dumps(
        filters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(tenant_id: str, fingerprint: str, snapshot: int, after: int) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "tenant": tenant_id,
        "filters": fingerprint,
        "snapshot": snapshot,
        "after": after,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: Any) -> dict:
    if not isinstance(cursor, str) or not cursor:
        raise AuditCursorError("cursor is malformed")
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise AuditCursorError("cursor is malformed") from None
    if not isinstance(payload, dict):
        raise AuditCursorError("cursor is malformed")
    snapshot = payload.get("snapshot")
    after = payload.get("after")
    if (
        payload.get("v") != _CURSOR_VERSION
        or not isinstance(payload.get("tenant"), str)
        or not isinstance(payload.get("filters"), str)
        or isinstance(snapshot, bool)
        or not isinstance(snapshot, int)
        or isinstance(after, bool)
        or not isinstance(after, int)
        or snapshot < 0
        or after < 0
    ):
        raise AuditCursorError("cursor is malformed")
    if after > snapshot:
        raise AuditCursorError("cursor is outside its snapshot range")
    return payload


class AuditStore:
    """进程内多租户审计存储。

    序号在每个租户内从 1 开始严格递增且永不复用；不同租户的事件与
    序号完全隔离，任何查询结果都不会泄露其他租户的存在或规模。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # tenant_id -> 按序号升序排列的内部事件列表
        self._events: dict[str, list[dict]] = {}
        # tenant_id -> 下一个待分配序号
        self._next_sequence: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(
        self,
        tenant_id: str,
        event_type: str,
        occurred_at: Any,
        result: str,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        details: dict | None = None,
    ) -> dict:
        """写入一条审计事件，返回与后续查询一致的记录。

        校验失败抛出 :class:`AuditValidationError` 且不产生部分记录；
        详情在写入时深拷贝，调用方之后修改原对象不影响已保存事件。
        """
        tenant_id = _require_non_empty_str(tenant_id, "tenant_id")
        event_type = _require_non_empty_str(event_type, "event_type")
        result = _require_non_empty_str(result, "result")
        moment = _coerce_time(occurred_at, "occurred_at")
        optional = {
            "device_id": _optional_str(device_id, "device_id"),
            "session_id": _optional_str(session_id, "session_id"),
            "correlation_id": _optional_str(correlation_id, "correlation_id"),
        }
        stored_details = _validate_details(details)

        with self._lock:
            sequence = self._next_sequence.get(tenant_id, 1)
            event = {
                "sequence": sequence,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "occurred_at": moment,
                "result": result,
            }
            for key, value in optional.items():
                if value is not None:
                    event[key] = value
            if stored_details is not None:
                event["details"] = stored_details
            self._events.setdefault(tenant_id, []).append(event)
            self._next_sequence[tenant_id] = sequence + 1
            return self._public_record(event)

    # 便于不同调用习惯的别名。
    append_event = append
    record_event = append

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        tenant_id: str,
        *,
        device_id: str | None = None,
        event_type: str | None = None,
        result: str | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = PAGE_SIZE_DEFAULT,
        cursor: str | None = None,
    ) -> dict:
        """按序号升序分页查询单个租户的事件。

        返回 ``{"events": [...], "next_cursor": str | None}``。游标为
        不透明字符串，记录了查询起始时的快照上界，翻页期间新写入的
        事件不会混入后续页，也不会产生重复。
        """
        tenant_id = _require_non_empty_str(tenant_id, "tenant_id")
        limit = _validate_limit(limit)
        filters = self._build_filters(
            device_id=device_id,
            event_type=event_type,
            result=result,
            start=start,
            end=end,
        )
        fingerprint = _filters_fingerprint(filters["canonical"])

        with self._lock:
            events = self._events.get(tenant_id, ())
            if cursor is None:
                snapshot = events[-1]["sequence"] if events else 0
                after = 0
            else:
                payload = _decode_cursor(cursor)
                if payload["tenant"] != tenant_id:
                    raise AuditCursorError("cursor belongs to a different tenant")
                if payload["filters"] != fingerprint:
                    raise AuditCursorError("cursor does not match the query filters")
                snapshot = payload["snapshot"]
                after = payload["after"]

            matched = [
                event
                for event in events
                if after < event["sequence"] <= snapshot
                and self._matches(event, filters)
            ]
            page = matched[:limit]
            next_cursor = None
            if len(matched) > limit:
                next_cursor = _encode_cursor(
                    tenant_id, fingerprint, snapshot, page[-1]["sequence"]
                )
            return {
                "events": [self._public_record(event) for event in page],
                "next_cursor": next_cursor,
            }

    query_events = query

    # ------------------------------------------------------------------
    # 诊断导出
    # ------------------------------------------------------------------

    def export(
        self,
        tenant_id: str,
        *,
        device_id: str | None = None,
        event_type: str | None = None,
        result: str | None = None,
        start: Any = None,
        end: Any = None,
    ) -> dict:
        """导出诊断数据：UTF-8 JSON Lines，每行一个事件。

        返回 ``{"content": bytes, "record_count": int, "sha256": str}``。
        详情中的敏感键被递归遮蔽为 ``[REDACTED]``，存储中的原始事件
        不受影响。无法序列化时抛出 :class:`AuditValidationError`，
        不返回残缺导出。
        """
        tenant_id = _require_non_empty_str(tenant_id, "tenant_id")
        filters = self._build_filters(
            device_id=device_id,
            event_type=event_type,
            result=result,
            start=start,
            end=end,
        )

        with self._lock:
            matched = [
                event
                for event in self._events.get(tenant_id, ())
                if self._matches(event, filters)
            ]
            lines = []
            for event in matched:
                record = self._public_record(event)
                if "details" in record:
                    record["details"] = _redact(record["details"])
                try:
                    lines.append(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                except (TypeError, ValueError):
                    raise AuditValidationError(
                        "event details must be JSON-serializable"
                    ) from None
            content = "".join(line + "\n" for line in lines).encode("utf-8")
            return {
                "content": content,
                "record_count": len(matched),
                "sha256": hashlib.sha256(content).hexdigest(),
            }

    export_diagnostics = export

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filters(
        *,
        device_id: str | None,
        event_type: str | None,
        result: str | None,
        start: Any,
        end: Any,
    ) -> dict:
        start_at = _coerce_time(start, "start") if start is not None else None
        end_at = _coerce_time(end, "end") if end is not None else None
        return {
            "device_id": _optional_str(device_id, "device_id"),
            "event_type": _optional_str(event_type, "event_type"),
            "result": _optional_str(result, "result"),
            "start": start_at,
            "end": end_at,
            "canonical": {
                "device_id": device_id,
                "event_type": event_type,
                "result": result,
                "start": _format_time(start_at) if start_at is not None else None,
                "end": _format_time(end_at) if end_at is not None else None,
            },
        }

    @staticmethod
    def _matches(event: dict, filters: dict) -> bool:
        for key in ("device_id", "event_type", "result"):
            expected = filters[key]
            if expected is not None and event.get(key) != expected:
                return False
        if filters["start"] is not None and event["occurred_at"] < filters["start"]:
            return False
        if filters["end"] is not None and event["occurred_at"] > filters["end"]:
            return False
        return True

    @staticmethod
    def _public_record(event: dict) -> dict:
        record = {
            "sequence": event["sequence"],
            "tenant_id": event["tenant_id"],
            "event_type": event["event_type"],
            "occurred_at": _format_time(event["occurred_at"]),
            "result": event["result"],
        }
        for key in _OPTIONAL_FIELDS:
            if key in event:
                record[key] = event[key]
        if "details" in event:
            record["details"] = copy.deepcopy(event["details"])
        return record


def create_audit_store() -> AuditStore:
    """创建一个新的进程内多租户审计存储。"""
    return AuditStore()
