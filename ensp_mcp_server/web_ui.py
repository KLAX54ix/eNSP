"""
Embedded HTTP server for runtime SSE and local UI pages.
"""

from __future__ import annotations

import http.server
import json
import logging
import queue
import threading
import webbrowser
from pathlib import Path

from .config import collect_cleanup_device_names, registry_snapshot, remove_topology_devices, sync_topology_devices
from .console import disconnect_active_devices
from .runtime import publish, snapshot, subscribe

logger = logging.getLogger("ensp_mcp.web_ui")

PORT = 18080
ROOT_DIR = Path(__file__).resolve().parent.parent
TOPO_JSON = ROOT_DIR / "topology.json"

_server_lock = threading.Lock()
_server: http.server.ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None


def _load_topology_file() -> dict[str, object] | None:
    if not TOPO_JSON.exists():
        return None
    with TOPO_JSON.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("topology.json must contain a JSON object")
    return payload


def _save_topology_file(data: dict[str, object]) -> None:
    temp_file = TOPO_JSON.with_suffix(TOPO_JSON.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temp_file.replace(TOPO_JSON)


def _normalize_devices() -> list[dict[str, object]]:
    devices: list[dict[str, object]] = []
    for name, entry in registry_snapshot().items():
        if isinstance(entry, dict):
            port = int(entry.get("port", 23))
            username = entry.get("username", "")
            registry_source = str(entry.get("_source", "registry"))
        else:
            port = int(entry)
            username = ""
            registry_source = "legacy"
        devices.append(
            {
                "name": name,
                "port": port,
                "username": username,
                "host": "127.0.0.1",
                "source": "registry",
                "registrySource": registry_source,
            }
        )
    devices.sort(key=lambda item: str(item["name"]).lower())
    return devices


class UIRequestHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def guess_type(self, path):
        guessed = super().guess_type(path)
        if guessed == "text/html":
            return "text/html; charset=utf-8"
        return guessed

    def do_GET(self):
        if self.path in {"", "/"}:
            self.send_response(302)
            self.send_header("Location", "/index.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/api/topology":
            if TOPO_JSON.exists():
                try:
                    self.send_json(200, _load_topology_file())
                except (json.JSONDecodeError, OSError, ValueError) as exc:
                    self.send_json(500, {"error": f"invalid topology.json: {exc}"})
            else:
                self.send_json(404, {"error": "no topology loaded yet"})
            return

        if self.path == "/api/devices":
            self.send_json(200, {"devices": _normalize_devices()})
            return

        if self.path == "/api/runtime":
            self.send_json(200, snapshot())
            return

        if self.path == "/api/history":
            self.send_json(200, {"runs": snapshot().get("recent_requests", [])})
            return

        if self.path == "/api/runtime/stream":
            self._stream_runtime()
            return

        super().do_GET()

    def do_POST(self):
        if self.path == "/api/topology":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("topology payload must be a JSON object")
                devices = data.get("devices", [])
                links = data.get("links", [])
                if not isinstance(devices, list) or not isinstance(links, list):
                    raise ValueError("topology payload requires devices and links arrays")
                _save_topology_file(data)
                sync_summary = sync_topology_devices(devices)
                publish(
                    "topology_synced",
                    text=f"Topology synced: {sync_summary['count']} devices ready",
                    payload={
                        "device_count": len(devices),
                        "link_count": len(links),
                        "registry_count": sync_summary["count"],
                        "created": sync_summary["created"],
                        "updated": sync_summary["updated"],
                        "skipped": sync_summary["skipped"],
                    },
                    extra={"status": "done"},
                )
                self.send_json(200, {"ok": True, "path": str(TOPO_JSON), "registry": sync_summary})
                logger.info(
                    "topology.json saved (%s devices, %s links)",
                    len(devices),
                    len(links),
                )
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/topology/clear":
            try:
                payload: dict[str, object] = {}
                if TOPO_JSON.exists():
                    try:
                        payload = _load_topology_file() or {}
                    except (json.JSONDecodeError, OSError, ValueError):
                        payload = {}

                payload_devices = payload.get("devices", []) if isinstance(payload, dict) else []
                topology_targets = collect_cleanup_device_names(payload_devices)
                if TOPO_JSON.exists():
                    TOPO_JSON.unlink()
                cleanup_targets = sorted(topology_targets)
                disconnect_summary = disconnect_active_devices(cleanup_targets)
                removed = remove_topology_devices(payload_devices)
                removed_total = removed["count"]
                publish(
                    "topology_cleared",
                    text=(
                        f"Topology cleared: disconnected {disconnect_summary['count']} active consoles, removed {removed_total} registry devices"
                        + (
                            f", restored {removed.get('restored_count', 0)} preserved entries"
                            if removed.get("restored_count", 0)
                            else ""
                        )
                    ),
                    payload={
                        "topology_cleanup": removed,
                        "registry_cleanup": {"removed": [], "skipped": [], "count": 0},
                        "count": removed_total,
                    },
                    extra={"status": "done"},
                )
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "registry": {
                            "topology_cleanup": removed,
                            "registry_cleanup": {"removed": [], "skipped": [], "count": 0},
                            "count": removed_total,
                        },
                        "connections": disconnect_summary,
                    },
                )
                logger.info("topology.json cleared (%s devices removed)", removed_total)
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})
            return

        self.send_json(404, {"error": "not found"})

    def _stream_runtime(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()

        with subscribe() as events:
            while True:
                try:
                    event = events.get(timeout=15)
                    payload = json.dumps(event, ensure_ascii=False)
                    body = f"id: {event['id']}\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(body)
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        first = args[0] if args else ""
        if isinstance(first, str) and "/api/" in first:
            return
        super().log_message(fmt, *args)


def start_ui_server(port: int = PORT, open_browser: bool = False) -> str | None:
    global _server, _server_thread
    with _server_lock:
        if _server is not None:
            url = f"http://127.0.0.1:{_server.server_port}"
            if open_browser:
                webbrowser.open(f"{url}/index.html")
            return url

        try:
            _server = http.server.ThreadingHTTPServer(("127.0.0.1", port), UIRequestHandler)
        except OSError:
            logger.exception("Unable to start embedded UI server on 127.0.0.1:%s", port)
            return None

        _server.daemon_threads = True
        url = f"http://127.0.0.1:{port}"

        def _serve() -> None:
            assert _server is not None
            logger.info("Embedded UI server running at %s", url)
            _server.serve_forever()

        _server_thread = threading.Thread(target=_serve, name="ensp-ui-server", daemon=True)
        _server_thread.start()

    if open_browser:
        webbrowser.open(f"{url}/index.html")
    return url
