from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from opendevice.ax import AXClient, AXElement
from opendevice.session import DeviceSession

from .h265dump import H265Dumper

AX_WEDGE_REBUILD_AFTER = 2

BUTTON_SPECS: List[Tuple[str, str, float]] = [
    ("home", "Home", 0.0),
    ("power", "Power/Lock", 0.35),
    ("volumeDown", "Vol-", 0.0),
    ("volumeUp", "Vol+", 0.0),
    ("mute", "Mute", 0.0),
    ("siri", "Siri (hold Power)", 1.5),
]


@dataclass(frozen=True)
class Snapshot:
    """State visible to the UI layer."""

    connected: bool
    streaming: bool
    controls_locked: bool
    status: str
    screen_px: Optional[Tuple[int, int]]
    ax_screen: Optional[Tuple[float, float]]
    ax_stale: bool
    stream_generation: int
    devmode: Optional[bool] = None
    image: Optional[str] = None
    paired: bool = False
    mount_note: str = ""
    info: Dict[str, Any] = field(default_factory=dict)


class Session:
    """An integrated session for one iOS device (synchronous API)."""

    def __init__(
        self,
        udid: Optional[str] = None,
        enable_video: bool = True,
        h265_path: Optional[str] = None,
        tunnel_backend: str = "auto",
        connection_type: str = "usb",
        force: bool = False,
    ) -> None:
        self.udid = udid
        self.force = force
        self.tunnel_backend = tunnel_backend
        self.enable_video = enable_video
        self.h265_path = h265_path
        self.connection_type = connection_type
        self._paused_by_policy = False

        self.dev: Optional[DeviceSession] = None
        self.rsd = None

        self.live: Optional[H265Dumper] = None
        self._stream_generation = 0
        self._ax_cache: List[AXElement] = []

        self.info: Dict[str, Any] = {}
        self.status = "not connected"
        self.screen_px: Optional[Tuple[int, int]] = None
        self.screen_points_from_display: Optional[Tuple[float, float]] = None
        self.ax_screen: Optional[Tuple[float, float]] = None

        self._lock = threading.Lock()

        self._rebuild_lock = threading.Lock()
        self.devmode: Optional[bool] = None
        self.image: Optional[str] = None
        self.paired = False
        self.mount_note = ""
        self._mounting = False
        self._ax_wedged_hits = 0

        self._pending_units: List[Tuple[bytes, bool]] = []
        self._collecting = False

        self._usb_stop = threading.Event()
        self._usb_thread: Optional[threading.Thread] = None

    def connect(self) -> "Session":
        """Connect device -> read display info -> start the stream."""
        self.dev = DeviceSession(udid=self.udid, link=self.tunnel_backend,
                                 force=self.force)
        self.dev.on_status = self._on_status
        self.dev.on_error = self._on_error
        self.dev.connect()
        self.rsd = self.dev.rsd
        if self.dev.udid:
            self.udid = self.dev.udid

        self.info = {
            "product_type": self.dev.product_type,
            "product_version": self.dev.product_version,
        }
        self._load_display()

        if self.enable_video:
            self._start_video()

        self.preflight()
        self.status = (
            f"{self.info.get('product_type') or 'device'} "
            f"iOS {self.info.get('product_version') or '?'}"
        )

        if self._usb_thread is None or not self._usb_thread.is_alive():
            self._usb_stop.clear()
            self._usb_thread = threading.Thread(target=self._usb_watch, daemon=True)
            self._usb_thread.start()
        return self

    def preflight(self) -> dict[str, Any]:
        """Prerequisites for screen mirroring / touch / control tree: developer mode, personalized image, remote pairing (Wi-Fi only)."""
        from opendevice.core import paths

        self.devmode = None
        self.image = None
        self.info.pop("preflight_error", None)
        if self.dev is not None:
            try:
                from opendevice.services.mounter import MobileImageMounter

                with MobileImageMounter.open(self.dev.lockdown, self.rsd) as mounter:
                    self.devmode = mounter.developer_mode_status()
                    self.image = mounter.mounted_path()
            except Exception as exc:                 # noqa: BLE001 - if it cannot be queried, treat as not queried
                self.info["preflight_error"] = str(exc)[:200]
        self.paired = False
        if self.udid:
            self.paired = any(item.is_file()
                              for item in paths.record_paths(identifier=self.udid))
        return {"devmode": self.devmode, "image": self.image, "paired": self.paired}

    def ecid(self) -> Optional[int]:
        """The device ECID (needed to sign TSS)."""
        if self.dev is None or self.dev.lockdown is None:
            return None
        try:
            return int(self.dev.lockdown.all_values().get("UniqueChipID"))
        except Exception:                            # noqa: BLE001 - if unavailable, let mount say so
            return None

    def _note(self, text: str) -> None:
        """Progress of pushing the image (written to :attr:`mount_note`, shown in the status bar)."""
        self.mount_note = text

    def ensure_image(self) -> bool:
        """If no image is mounted, push one up (background thread, does not block the UI)."""
        if self._mounting or self.dev is None or self.image:
            return False
        self._mounting = True
        self.mount_note = "preparing to push the image..."

        def run() -> None:
            try:
                from opendevice.services.mounter import MobileImageMounter

                with MobileImageMounter.open(self.dev.lockdown, self.rsd) as mounter:
                    mounter.mount(product_type=self.info.get("product_type"),
                                  version=self.info.get("product_version"),
                                  ecid=self.ecid(), on_status=self._note)
                self.mount_note = "image mounted, rebuilding the link..."
                self.rebuild_link("image mounted")
                self.mount_note = "image mounted"
            except Exception as exc:                 # noqa: BLE001 - show the user why it failed
                self.mount_note = f"failed to push the image: {exc}"
            finally:
                self._mounting = False

        threading.Thread(target=run, daemon=True, name="webui-mount").start()
        return True

    def pair_remote(self, force: bool = False) -> str:
        """Establish remote pairing over USB (a prerequisite for the Wi-Fi link): use the already-trusted lockdown, no prompt."""
        from opendevice.core import paths
        from opendevice.wireless.rrp import pair_over_lockdown

        dev = self._require_dev()
        if dev.lockdown is None:
            raise RuntimeError("remote pairing must be done over USB: currently on Wi-Fi, switch to USB first")
        if force:
            for path in paths.unpair(all_records=True):
                self._note(f"deleting old record {path}")
        record = pair_over_lockdown(dev.lockdown, udid=self.udid)
        self.paired = True
        return str(record.path)

    def unpair(self, all_records: bool = False) -> list[str]:
        """Delete this host's remote pairing records (this host's side of Unpair)."""
        from opendevice.core import paths

        removed = paths.unpair(all_records=all_records) if all_records else \
            paths.unpair(identifier=self.udid)
        self.paired = False
        self.mount_note = ""
        return [str(item) for item in removed]

    def _load_display(self) -> None:
        """Read display info: pixel size + pointScale (DeviceSession already fetched once; cache it here)."""
        if self.dev is None:
            return
        try:
            pixels = self.dev.screen_px
            if pixels:
                self.screen_px = (int(pixels[0]), int(pixels[1]))
            try:
                points = self.dev.screen_points()
            except Exception:                    # noqa: BLE001 - fall back to a default ratio if unavailable
                points = None
            if points:
                self.screen_points_from_display = (float(points[0]), float(points[1]))
        except Exception as exc:                 # noqa: BLE001
            self.info["display_error"] = str(exc)[:120]

    def _start_video(self) -> None:
        """Start the stream: let DeviceSession negotiate, then build the sink, then replay the buffered units."""
        if self.dev is None:
            raise RuntimeError("device not connected")

        self._pending_units = []
        self._collecting = True
        self.dev.start_stream(on_access_unit=self._on_access_unit)

        sink = H265Dumper(self.h265_path)
        sink.start()
        self.live = sink
        self._stream_generation += 1

        pending, self._pending_units = self._pending_units, []
        self._collecting = False
        for data, is_key in pending:
            sink.feed(data, is_key)

    def _on_access_unit(self, data: bytes, is_key: bool) -> None:
        """RTP receive-thread callback: one H.265 access unit (already Annex-B)."""
        if self._collecting:
            self._pending_units.append((data, is_key))
            return
        sink = self.live
        if sink is not None:
            sink.feed(data, is_key)

    def _stop_video(self) -> None:
        """Stop the stream (leaves ``enable_video`` alone)."""
        sink, self.live = self.live, None
        self._stream_generation += 1
        self._collecting = False
        self._pending_units = []
        if self.dev is not None:
            self.dev.stop_stream()
        if sink is not None:
            try:
                sink.stop()
            except Exception:                # noqa: BLE001
                pass

    @property
    def h265(self) -> Optional[H265Dumper]:
        """The current stream sink (non-None only while streaming)."""
        return self.live

    def h265_stats(self) -> Optional[dict[str, Any]]:
        sink = self.h265
        return sink.stats() if sink else None

    @property
    def stream_generation(self) -> int:
        """The stream's **generation** (+1 each time a sink is built/torn down)."""
        return self._stream_generation

    def snapshot(self) -> "Snapshot":
        """A single **snapshot of state** for the UI layer (the contract surface)."""
        return Snapshot(
            connected=self.rsd is not None,
            streaming=self.streaming,
            controls_locked=self.controls_locked,
            status=self.status,
            screen_px=self.screen_px,
            ax_screen=self.ax_screen,
            ax_stale=bool(self.info.get("ax_stale")),
            stream_generation=self._stream_generation,
            devmode=self.devmode,
            image=self.image,
            paired=self.paired,
            mount_note=self.mount_note,
            info=dict(self.info),
        )

    def request_keyframe(self) -> bool:
        """Ask the device for an IDR (for new subscribers / after a decode error)."""
        return bool(self.dev is not None and self.dev.request_keyframe())

    def rtp_stats(self) -> Optional[dict[str, Any]]:
        """RTP receive stats (lost / reordered packets make decoders complain)."""
        if self.dev is None or not self.dev.streaming:
            return None
        return dict(self.dev.stream_stats)

    def _on_error(self, text: str) -> None:
        self.status = f"screen: {text}"

    def _on_status(self, text: str) -> None:
        """The stream state changed."""
        self.info["stream"] = text

    @property
    def hid(self) -> Any:
        """The touch client."""
        return self.dev.touch if self.dev is not None else None

    @property
    def kbd(self) -> Any:
        """The keyboard client."""
        return self.dev.touch if self.dev is not None else None

    ax: Optional[AXClient] = None

    def _require_dev(self) -> DeviceSession:
        if self.dev is None:
            raise RuntimeError("device not connected")
        return self.dev

    def tap(self, x: float, y: float) -> None:
        """Tap at **normalized** coordinates (0..1)."""
        self._require_dev().tap(x, y)

    def swipe(self, start: Tuple[float, float], end: Tuple[float, float],
              duration: float = 0.3) -> None:
        self._require_dev().swipe(start, end, duration=duration)

    def move(self, x: float, y: float) -> None:
        self._require_dev().move(x, y)

    def touch_event(self, phase: str, x: float, y: float) -> None:
        """One step of a live touch (``down`` / ``move`` / ``up``)."""
        self._require_dev().touch_event(phase, x, y)

    def move_cursor(self, x: float, y: float) -> None:
        """Move only the **device-side visible cursor** (the gray dot on the screen) without touching."""
        self._require_dev().move_cursor(x, y)

    def type_text(self, text: str) -> int:
        """Type (virtual HID keyboard); returns the number of characters sent."""
        return self._require_dev().type_text(text)

    def send_text(self, text: str) -> bool:
        """Send the whole string **directly** into the currently focused field (not char by char)."""
        if not text:
            return False
        self._require_dev().send_text(text, approve=True)
        return True

    def tap_point(self, x: float, y: float) -> None:
        """Tap at **screen-point** coordinates (converted to normalized internally)."""
        size = self.ax_screen or self.screen_points_from_display
        if not size or not size[0] or not size[1]:
            raise RuntimeError("cannot get the screen point size; tap with normalized coordinates first")
        self.tap(x / size[0], y / size[1])

    def tap_element(self, element: AXElement) -> bool:
        center = element.center
        if center is None:
            return False
        self.tap_point(*center)
        return True

    def _maybe_auto_rebuild(self, reason: str) -> None:
        """The device AX session has wedged -> **rebuild the whole link** in the background, no phone restart needed."""
        self._ax_wedged_hits += 1
        if self._ax_wedged_hits < AX_WEDGE_REBUILD_AFTER:
            return
        self._ax_wedged_hits = 0
        threading.Thread(target=self.rebuild_link, args=(reason,), daemon=True,
                         name="webui-rebuild").start()

    def elements(self, timeout: float = 15.0) -> List[AXElement]:
        """Fetch the control list of the current screen (with label + exact frame)."""
        usable = self._require_dev().elements(timeout=timeout)
        if self.dev is not None and self.dev.ax_wedged:
            self._maybe_auto_rebuild("control session wedged")
        if usable:
            self._ax_wedged_hits = 0
            self._ax_cache = usable
            self.info.pop("ax_error", None)
            self.info.pop("ax_stale", None)
            best = None
            for element in usable:
                if element.frame:
                    area = element.width * element.height
                    if best is None or area > best[0]:
                        best = (area, element.frame)
            if best:
                self.ax_screen = (best[1][2], best[1][3])
            return usable

        self.info["ax_stale"] = True
        return list(self._ax_cache)

    def press_button(self, name: str, hold: float = 0.0) -> Tuple[int, int]:
        """Press a hardware key. See :data:`BUTTONS` for ``name``, or pass ``"page,code"`` directly."""
        return self._require_dev().press_button(name, hold=hold or 0.08)

    def start_video(self) -> bool:
        """Start streaming. No-op (returns False) if already running."""
        if self.live is not None:
            return False
        self.enable_video = True
        self._start_video()
        self.status = (f"{self.info.get('product_type') or 'device'} "
                       f"iOS {self.info.get('product_version') or '?'}")
        return True

    def stop_video(self) -> bool:
        """Stop streaming and tear down the device-side session."""
        if self.live is None:
            return False
        self._stop_video()
        self.status = "screen: stopped"
        self.info.pop("stream", None)
        return True

    @property
    def streaming(self) -> bool:
        """Whether the stream is running."""
        return self.live is not None

    @property
    def controls_locked(self) -> bool:
        """Whether the controls are locked."""
        return self.connection_type == "usb" and self._paused_by_policy

    def switch_transport(self, new_type: str) -> bool:
        """Switch link: ``"usb"`` / ``"wifi"``. Returns whether the link changed."""
        new_type = (new_type or "").strip().lower()
        if new_type not in ("usb", "wifi"):
            raise ValueError(f"link must be usb or wifi, got {new_type!r}")
        dev = self._require_dev()
        if not dev.link_state()["switchable"]:
            raise RuntimeError(
                "this tunnel cannot switch links (link is externally injected or the backend does not support it); "
                "start with --tunnel userspace / network to switch"
            )

        self._stop_video()
        try:
            changed = dev.switch_link(new_type)
        except Exception:
            if self.enable_video:
                self._start_video()
            raise
        self.connection_type = new_type
        self._paused_by_policy = False
        self._ax_wedged_hits = 0
        if self.enable_video:
            self._start_video()
        return bool(changed)

    def _current_link(self) -> str:
        """The link the tunnel is currently using."""
        if self.dev is None:
            return "wifi" if self.tunnel_backend == "network" else "usb"
        return str(self.dev.link_state()["kind"])

    def _probe_links(self) -> dict[str, Any]:
        """**Measured** (not policy): which links the device has now."""
        return {"usb": self._usb_attached(), "active": self._current_link()}

    def _teardown_clients(self) -> None:
        """Release the device-side session and clear references."""
        self.live = None
        self._ax_cache = []
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:                    # noqa: BLE001
                pass
            self.dev = None
        self.rsd = None

    def _usb_attached(self) -> Optional[bool]:
        """Whether the device is currently on USB."""
        if self.dev is None:
            return None
        return self.dev.usb_attached()

    def _usb_watch(self) -> None:
        """In usb mode, stop the stream as soon as the cable is pulled instead of waiting for the frame watchdog's 24s timeout."""
        while not self._usb_stop.is_set():
            time.sleep(2)
            if self.connection_type != "usb":
                continue
            attached = self._usb_attached()
            if attached is False and self.streaming:
                self.stop_video()
                self._paused_by_policy = True
                self.status = "unplugged (usb-only policy): stream stopped, plug the cable back in to resume"
            elif attached is True and self._paused_by_policy:
                self._paused_by_policy = False
                self.start_video()
                self.status = "USB reconnected: stream resumed automatically"

    def transport_payload(self) -> dict[str, Any]:
        """Link state for the UI: the requested link and the one in effect (both read from the library)."""
        links = self._probe_links()
        active = links["active"]
        switchable = bool(self.dev is not None
                          and self.dev.link_state()["switchable"])
        if active == "wifi":
            label, note = "Wi-Fi", "bonjour + RemotePairing, unplug-safe"
        else:
            label, note = "USB", "usbmux, drops when unplugged"
        return {
            "type": self.connection_type,
            "transport": (self.dev.link_state()["backend"] if self.dev is not None
                          else self.tunnel_backend),
            "active": active,
            "usb_attached": links["usb"],
            "enforced": switchable,
            "network": active == "wifi",
            "label": label,
            "note": note,
            "switch_rebuilds": switchable,
        }

    def disconnect(self) -> bool:
        """Disconnect from the device."""
        if self.rsd is None and not self._tunnel_alive():
            return False
        self.stop_video()
        self._teardown_clients()
        self.status = "disconnected"
        self.info.pop("stream", None)
        return True

    def reconnect_video(self) -> None:
        """Reconnect: when the tunnel is still alive, restart only the stream."""
        if not self._tunnel_alive():
            self.stop_video()
            self._teardown_clients()
            self.connect()
            return
        self._stop_video()
        if self.enable_video:
            self._start_video()

    def rebuild_link(self, reason: str = "") -> bool:
        """Rebuild the **entire** link: stop stream -> drop clients -> drop tunnel -> reconnect (incl. restart the stream)."""
        if not self._rebuild_lock.acquire(blocking=False):
            return False
        try:
            self.status = f"rebuilding the whole link ({reason or 'reconnect'})..."
            self._stop_video()
            self._teardown_clients()
            self._ax_wedged_hits = 0
            self._paused_by_policy = False
            self.connect()
            self.status = f"link rebuilt ({reason or 'reconnect'})"
            return True
        except Exception as exc:               # noqa: BLE001
            self.status = f"whole-link rebuild failed: {exc}"
            raise
        finally:
            self._rebuild_lock.release()

    def _tunnel_alive(self) -> bool:
        """Whether the tunnel is up and RSD is still reachable."""
        return bool(self.dev is not None and self.dev.is_alive())

    def close(self) -> None:
        """Exit = disconnect: stop stream -> release device-side session -> drop tunnel (device is released)."""
        self._usb_stop.set()
        self._stop_video()
        self._teardown_clients()
        self.status = "disconnected"
        self.info.pop("stream", None)
