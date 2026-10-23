from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, cast

from .session import BUTTON_SPECS, Session

TYPE_DELTA = 0x01
TYPE_KEY = 0x04
TYPE_META = 0x03


def pack_frame(ftype: int, payload: bytes) -> bytes:
    """Pack one frame message (length includes the type byte, big-endian)."""
    body = bytes((ftype,)) + payload
    return len(body).to_bytes(4, "big") + body


PAGE_DIR = Path(__file__).resolve().parent / "page"

PAGE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "webui"

    @property
    def ui(self) -> "WebUI":
        """The WebUI attached to this server."""
        return cast("WebUI", cast(Any, self.server).ui)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in PAGE_FILES:
            self._page(*PAGE_FILES[path])
        elif path == "/stream.h265":
            self._h265()
        elif path == "/frames":
            self._frames()
        elif path == "/api/h265":
            self._json(self.ui.frame_meta())
        elif path == "/api/elements":
            self._json(self.ui.elements_payload())
        elif path == "/api/status":
            self._json(self.ui.status_payload())
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = {}
        try:
            ui = self.ui
            if path in ("/api/cursor", "/api/tap", "/api/point", "/api/swipe",
                        "/api/touch", "/api/button", "/api/type", "/api/sendtext") \
                    and ui.session.controls_locked:
                self._json({"ok": False,
                            "error": "screen is down (usb-only policy: USB unplugged); controls are locked"})
                return
            elif path == "/api/cursor":
                cx, cy = data.get("x"), data.get("y")
                if cx is None or cy is None:
                    self._json({"ok": False, "error": "missing x / y"})
                    return
                ui.session.move_cursor(float(cx), float(cy))
            elif path == "/api/touch":
                tx, ty = data.get("x"), data.get("y")
                if tx is None or ty is None:
                    self._json({"ok": False, "error": "missing x / y"})
                    return
                ui.session.touch_event(str(data.get("phase", "move")),
                                       float(tx), float(ty))
            elif path == "/api/tap":
                ui.session.tap(float(data["x"]), float(data["y"]))
            elif path == "/api/point":
                ui.session.tap_point(float(data["x"]), float(data["y"]))
            elif path == "/api/swipe":
                ui.session.swipe((float(data["x0"]), float(data["y0"])),
                                 (float(data["x1"]), float(data["y1"])))
            elif path == "/api/button":
                ui.session.press_button(str(data.get("name", "")),
                                        hold=float(data.get("hold", 0.0)))
            elif path == "/api/type":
                ui.session.type_text(str(data.get("text", "")))
            elif path == "/api/sendtext":
                ui.session.send_text(str(data.get("text", "")))
            elif path == "/api/stream/start":
                ui.session.start_video()
            elif path == "/api/stream/stop":
                ui.session.stop_video()
            elif path == "/api/transport":
                changed = ui.session.switch_transport(str(data.get("type", "")))
                self._json({"ok": True, "changed": bool(changed),
                            "transport": ui.session.transport_payload()})
                return
            elif path == "/api/reconnect":
                ui.session.rebuild_link("manual reconnect")
            elif path == "/api/disconnect":
                ui.session.disconnect()
            elif path == "/api/keyframe":
                ui.session.request_keyframe()
            elif path == "/api/preflight":
                self._json(ui.session.preflight())
                return
            elif path == "/api/mount":
                started = ui.session.ensure_image()
                self._json({"ok": True, "started": started,
                            "note": ui.session.mount_note})
                return
            elif path == "/api/pairing":
                record_path = ui.session.pair_remote(force=bool(data.get("force")))
                self._json({"ok": True, "path": record_path,
                            "paired": ui.session.paired})
                return
            elif path == "/api/unpairing":
                removed = ui.session.unpair(all_records=bool(data.get("all")))
                self._json({"ok": True, "removed": removed,
                            "paired": ui.session.paired})
                return
            else:
                self.send_error(404)
                return
        except Exception as exc:             # noqa: BLE001
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True})

    def _page(self, name: str, content_type: str):
        """Read page assets from disk each time; edit and refresh the browser to see changes."""
        try:
            body = (PAGE_DIR / name).read_bytes()
        except OSError as exc:
            self.send_error(500, "page missing", explain=f"cannot read {name}: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _frames(self):
        """Keyframe-tagged stream: decoded by WebCodecs in the browser (the server does not decode)."""
        sink = self.ui.session.h265
        if sink is None:
            self.send_error(404, "no stream",
                            explain="not streaming (/frames is only available while the stream runs)")
            return
        self.send_response(200)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        gen = self.ui.session.stream_generation
        q = sink.subscribe()
        sent_meta = False
        sent_key = False
        self.ui.session.request_keyframe()
        last_ask = time.monotonic()
        try:
            while (not self.ui.stopping.is_set()
                   and self.ui.session.stream_generation == gen):
                if not sent_key and time.monotonic() - last_ask >= 2.0:
                    self.ui.session.request_keyframe()
                    last_ask = time.monotonic()
                if not sent_meta:
                    meta = self.ui.frame_meta()
                    if meta.get("codec"):
                        self.wfile.write(pack_frame(TYPE_META, json.dumps(meta).encode()))
                        sent_meta = True
                try:
                    is_key, data = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                sent_key = sent_key or is_key
                self.wfile.write(pack_frame(TYPE_KEY if is_key else TYPE_DELTA, data))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sink.unsubscribe(q)

    def _h265(self):
        """Raw Annex-B stream live (no decoding); open directly in mpv / VLC."""
        sink = self.ui.session.h265
        if sink is None:
            self.send_error(404, "no h265 dump (use --dump-h265)")
            return
        self.send_response(200)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Type", "video/x-h265")
        self.end_headers()
        gen = self.ui.session.stream_generation
        q = sink.subscribe()
        try:
            while (not self.ui.stopping.is_set()
                   and self.ui.session.stream_generation == gen):
                try:
                    _is_key, data = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sink.unsubscribe(q)

class _Server(ThreadingHTTPServer):
    """Owns the listening port."""

    allow_reuse_address = os.name != "nt"

    ui: Any = None


class WebUI:
    """Expose :class:`~webui.session.Session` as a local web page."""

    def __init__(self, session: Session, bind: str = "127.0.0.1", port: int = 8800) -> None:
        self.session = session
        self.bind = bind
        self.port = port
        self.stopping = threading.Event()
        self._server: Optional[ThreadingHTTPServer] = None

    def elements_payload(self) -> dict[str, Any]:
        snap = self.session.snapshot()
        try:
            elements = self.session.elements()
        except Exception as exc:             # noqa: BLE001
            return {"elements": [], "screen": snap.ax_screen, "error": str(exc)}
        return {
            "screen": snap.ax_screen,
            "stale": snap.ax_stale,
            "elements": [
                {"text": el.text, "kind": el.kind, "frame": el.frame,
                 "center": el.center, "enabled": el.enabled}
                for el in elements if el.frame
            ],
        }

    def frame_meta(self) -> dict[str, Any]:
        """Metadata the browser needs to decode: codec string + frame size."""
        sink = self.session.h265
        size = self.session.screen_px or (0, 0)
        return {
            "active": sink is not None,
            "codec": sink.codec if sink else None,
            "width": size[0],
            "height": size[1],
        }

    def status_payload(self) -> dict[str, Any]:
        snap = self.session.snapshot()
        return {
            "screen": snap.ax_screen,
            "info": snap.info,
            "status": snap.status,
            "h265": self.session.h265_stats(),
            "rtp": self.session.rtp_stats(),
            "streaming": snap.streaming,
            "connected": snap.connected,
            "controls_locked": snap.controls_locked,
            "transport": self.session.transport_payload(),
            "devmode": snap.devmode,
            "image": snap.image,
            "paired": snap.paired,
            "mount_note": snap.mount_note,
            "buttons": [{"name": name, "label": label, "hold": hold}
                        for name, label, hold in BUTTON_SPECS],
        }

    def serve_forever(self, open_browser: bool = True) -> None:
        httpd = _Server((self.bind, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.ui = self
        self._server = httpd
        url = f"http://{self.bind}:{self.port}/"
        if open_browser:
            import webbrowser
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stopping.set()
            try:
                httpd.shutdown()
            except Exception:                # noqa: BLE001
                pass
