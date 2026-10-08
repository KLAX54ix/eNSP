from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from queue import Empty, Queue
from typing import Any, Iterator
from uuid import uuid4

from .config import _device_registry, _get_device_port


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    if not arguments:
        return {}

    summary: dict[str, Any] = {}
    for key in ("device_ip", "device_names", "target_ip", "command", "commands", "section", "count", "search_dir", "file_path"):
        if key in arguments:
            summary[key] = arguments[key]

    if "devices" in arguments and isinstance(arguments["devices"], list):
        summary["devices"] = [
            {
                "device_ip": item.get("device_ip"),
                "command_count": len(item.get("commands", [])),
            }
            for item in arguments["devices"]
            if isinstance(item, dict)
        ]

    return summary


def _extract_targets(arguments: dict[str, Any] | None) -> list[str]:
    if not arguments:
        return []

    targets: list[str] = []

    device_ip = arguments.get("device_ip")
    if isinstance(device_ip, str) and device_ip:
        targets.append(device_ip)

    device_names = arguments.get("device_names")
    if isinstance(device_names, list):
        targets.extend([name for name in device_names if isinstance(name, str) and name])

    devices = arguments.get("devices")
    if isinstance(devices, list):
        for item in devices:
            if isinstance(item, dict):
                name = item.get("device_ip")
                if isinstance(name, str) and name:
                    targets.append(name)

    return targets


def _resolve_device_label(host: str | None, port: int | None) -> str | None:
    if not host and port is None:
        return None

    if host in {"127.0.0.1", "localhost"} and port is not None:
        for name, entry in _device_registry.items():
            if _get_device_port(entry) == port:
                return name
        return f"{host}:{port}"

    if host and port is not None:
        return f"{host}:{port}"
    return host or None


_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ensp_request_id", default=None)
_tool_name_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ensp_tool_name", default=None)
_target_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ensp_target", default=None)


@dataclass
class RuntimeRequest:
    request_id: str
    tool: str
    status: str
    started_at: str
    started_at_epoch: float
    arguments: dict[str, Any]
    targets: list[str]
    events: list[dict[str, Any]]
    result_preview: str = ""
    error: str = ""
    ended_at: str | None = None
    duration_ms: int | None = None


class RuntimeStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = count(1)
        self._subscribers: set[Queue[dict[str, Any]]] = set()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=500)
        self._recent_requests: deque[dict[str, Any]] = deque(maxlen=100)
        self._active_requests: dict[str, RuntimeRequest] = {}
        self._event_counter = 0

    @property
    def mcp_online(self) -> bool:
        return True

    def open_request(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        request_id = uuid4().hex
        record = RuntimeRequest(
            request_id=request_id,
            tool=tool,
            status="running",
            started_at=_utc_now(),
            started_at_epoch=time.time(),
            arguments=_summarize_arguments(arguments),
            targets=_extract_targets(arguments),
            events=[],
        )
        with self._lock:
            self._active_requests[request_id] = record
        return request_id

    def finish_request(self, request_id: str, status: str, result: str = "", error: str = "") -> None:
        with self._lock:
            record = self._active_requests.pop(request_id, None)
            if record is None:
                return

            record.status = status
            record.result_preview = result[:500] if result else ""
            record.error = error[:500] if error else ""
            record.ended_at = _utc_now()
            record.duration_ms = max(0, int((time.time() - record.started_at_epoch) * 1000))

            self._recent_requests.append(
                {
                    "request_id": record.request_id,
                    "tool": record.tool,
                    "status": record.status,
                    "started_at": record.started_at,
                    "ended_at": record.ended_at,
                    "duration_ms": record.duration_ms,
                    "arguments": deepcopy(record.arguments),
                    "targets": list(record.targets),
                    "result_preview": record.result_preview,
                    "error": record.error,
                }
            )

    def publish(
        self,
        event_type: str,
        *,
        text: str = "",
        device: str | None = None,
        tool: str | None = None,
        payload: dict[str, Any] | None = None,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or _request_id_var.get()
        tool = tool or _tool_name_var.get()
        if device is None:
            device = _target_var.get()

        event = {
            "id": f"evt-{next(self._seq)}",
            "ts": _utc_now(),
            "type": event_type,
            "request_id": request_id,
            "tool": tool,
            "device": device,
            "text": text,
            "payload": payload or {},
        }
        if extra:
            event.update(extra)

        with self._lock:
            self._event_counter += 1
            event["seq"] = self._event_counter
            self._recent_events.append(event)
            if request_id and request_id in self._active_requests:
                self._active_requests[request_id].events.append(event)
                if len(self._active_requests[request_id].events) > 120:
                    self._active_requests[request_id].events = self._active_requests[request_id].events[-120:]

            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(event)
                except Exception:
                    try:
                        queue.get_nowait()
                    except Empty:
                        pass
                    try:
                        queue.put_nowait(event)
                    except Exception:
                        pass

        return event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_requests = [
                {
                    "request_id": item.request_id,
                    "tool": item.tool,
                    "status": item.status,
                    "started_at": item.started_at,
                    "arguments": deepcopy(item.arguments),
                    "targets": list(item.targets),
                    "events": deepcopy(item.events[-40:]),
                }
                for item in self._active_requests.values()
            ]
            active_requests.sort(key=lambda item: item["started_at"])

            active_request = active_requests[0] if active_requests else None

            return {
                "mcp_online": self.mcp_online,
                "server_time": _utc_now(),
                "active_request": active_request,
                "active_requests": active_requests,
                "recent_requests": deepcopy(list(self._recent_requests)),
                "recent_events": deepcopy(list(self._recent_events)),
                "recent_logs": [
                    {
                        "id": event["id"],
                        "ts": event["ts"],
                        "type": event["type"],
                        "request_id": event["request_id"],
                        "tool": event["tool"],
                        "device": event["device"],
                        "text": event["text"],
                    }
                    for event in self._recent_events
                ],
            }

    @contextlib.contextmanager
    def request_scope(self, tool: str, arguments: dict[str, Any] | None = None) -> Iterator[str]:
        request_id = self.open_request(tool, arguments)
        request_token = _request_id_var.set(request_id)
        tool_token = _tool_name_var.set(tool)
        target_tokens: list[contextvars.Token[Any]] = []
        try:
            targets = _extract_targets(arguments)
            target = targets[0] if targets else None
            if target is not None:
                target_tokens.append(_target_var.set(target))
            yield request_id
        finally:
            if target_tokens:
                _target_var.reset(target_tokens.pop())
            _tool_name_var.reset(tool_token)
            _request_id_var.reset(request_token)

    @contextlib.contextmanager
    def device_scope(self, device: str | None) -> Iterator[None]:
        token = _target_var.set(device) if device else None
        try:
            yield
        finally:
            if token is not None:
                _target_var.reset(token)

    def subscribe(self) -> contextlib.AbstractContextManager[Queue[dict[str, Any]]]:
        @contextlib.contextmanager
        def _subscription() -> Iterator[Queue[dict[str, Any]]]:
            queue: Queue[dict[str, Any]] = Queue(maxsize=512)
            with self._lock:
                self._subscribers.add(queue)
            try:
                yield queue
            finally:
                with self._lock:
                    self._subscribers.discard(queue)

        return _subscription()

    def resolve_device_label(self, host: str | None, port: int | None) -> str | None:
        return _resolve_device_label(host, port)


runtime = RuntimeStore()


def publish(event_type: str, *, text: str = "", device: str | None = None, tool: str | None = None, payload: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return runtime.publish(event_type, text=text, device=device, tool=tool, payload=payload, extra=extra)


def snapshot() -> dict[str, Any]:
    return runtime.snapshot()


def request_scope(tool: str, arguments: dict[str, Any] | None = None) -> contextlib.AbstractContextManager[str]:
    return runtime.request_scope(tool, arguments)


def device_scope(device: str | None) -> contextlib.AbstractContextManager[None]:
    return runtime.device_scope(device)


def subscribe() -> contextlib.AbstractContextManager[Queue[dict[str, Any]]]:
    return runtime.subscribe()


def finish_request(request_id: str, status: str, result: str = "", error: str = "") -> None:
    runtime.finish_request(request_id, status, result=result, error=error)


def resolve_device_label(host: str | None, port: int | None) -> str | None:
    return runtime.resolve_device_label(host, port)
