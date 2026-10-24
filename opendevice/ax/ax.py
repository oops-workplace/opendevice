from __future__ import annotations

import plistlib
import re
import time
from typing import Any, Optional

from ..core.errors import OpenDeviceError
from ..remote.rsd import RSD
from .dtx import DISPATCH, DTXConnection

SERVICE_AX = "com.apple.accessibility.axAuditDaemon.remoteAXService"

ATTR_LABEL = "3"
ATTR_VALUE = "64"
ATTR_KIND = "45"
ATTR_FRAME = "21"
ATTR_FRAME_SCREEN = "48"
ATTR_CHILDREN = "9"
ATTR_CHILDREN_NAV = "8"
ATTR_TRAITS = "77"
ATTR_ENABLED = "31"
ATTR_DESC = "33"
ATTR_USER_LABELS = "87"
ATTR_ROLE_DESC = "78"
ATTR_PLACEHOLDER = "42"

KIND_NAMES = {1: 'Window', 2: 'Button', 4: 'Tab', 14: 'Text'}

STALE_DUMP_SIZE = 4000

#: One fetch is several payloads: ``InitialDump`` opens a snapshot, ``AdditionalData`` fills it in.
DUMP_INITIAL = "AXPTreeDumpTypeInitialDump"
DUMP_ADDITIONAL = "AXPTreeDumpTypeAdditionalData"

#: How long the stream has to stay quiet before a dump is considered finished.
QUIET_WINDOW = 1.1

#: How long one poll of the socket may block; also the tail overshoot after the last payload.
POLL_TICK = 0.1

_RECT_RE = re.compile(r"\{\{([-\d.eE+]+),\s*([-\d.eE+]+)\},\s*\{([-\d.eE+]+),\s*([-\d.eE+]+)\}\}")


def parse_rect(value: Any) -> Optional[tuple[float, float, float, float]]:
    "``'{ {x, y}, {w, h} }'`` -> (x, y, w, h)."
    if not isinstance(value, str):
        return None
    match = _RECT_RE.search(value)
    if not match:
        return None
    try:
        x, y, w, h = (float(g) for g in match.groups())
    except ValueError:
        return None
    return x, y, w, h


def _text_of(value: Any) -> str:
    'Coerce an attribute into a string (an array contributes its first non-empty entry).'
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item), "")
    return "" if value is None else str(value)


def _role_text(value: Any) -> str:
    'Join a role-description array into one ``·``-separated string.'
    if isinstance(value, (list, tuple)):
        return "·".join(str(item) for item in value if item)
    return "" if value is None else str(value)


class AXElement:
    'One accessibility element.'

    __slots__ = ("object_id", "pid", "label", "description", "value", "kind_code",
                 "frame", "children", "traits", "enabled", "user_labels", "role",
                 "placeholder", "raw")

    def __init__(self, object_id: int, pid: Optional[int], raw: dict[str, Any]) -> None:
        self.object_id = object_id
        self.pid = pid
        self.raw = raw
        self.label = _text_of(raw.get(ATTR_LABEL))
        self.description = _text_of(raw.get(ATTR_DESC))
        self.value = _text_of(raw.get(ATTR_VALUE))
        self.placeholder = _text_of(raw.get(ATTR_PLACEHOLDER))
        self.role = _role_text(raw.get(ATTR_ROLE_DESC))
        self.kind_code = raw.get(ATTR_KIND)
        self.traits = raw.get(ATTR_TRAITS)
        self.enabled = raw.get(ATTR_ENABLED)
        labels = raw.get(ATTR_USER_LABELS)
        self.user_labels = labels if isinstance(labels, list) else []
        self.frame = parse_rect(raw.get(ATTR_FRAME_SCREEN)) or parse_rect(raw.get(ATTR_FRAME))
        kids = raw.get(ATTR_CHILDREN) or []
        self.children: list[int] = [
            c[1] for c in kids
            if isinstance(c, tuple) and len(c) > 1 and isinstance(c[1], int)
        ]

    @property
    def x(self) -> Optional[float]:
        return self.frame[0] if self.frame else None

    @property
    def y(self) -> Optional[float]:
        return self.frame[1] if self.frame else None

    @property
    def width(self) -> Optional[float]:
        return self.frame[2] if self.frame else None

    @property
    def height(self) -> Optional[float]:
        return self.frame[3] if self.frame else None

    @property
    def center(self) -> Optional[tuple[float, float]]:
        if not self.frame:
            return None
        x, y, w, h = self.frame
        return x + w / 2.0, y + h / 2.0

    @property
    def text(self) -> str:
        return self.label or self.description or self.value or self.placeholder or ""

    @property
    def kind(self) -> str:
        'Element kind name; falls back to the first device role description.'
        name = KIND_NAMES.get(self.kind_code, "") if isinstance(self.kind_code, int) else ""
        if name:
            return name
        return self.role.split("·")[0] if self.role else ""

    def __repr__(self) -> str:
        return f"<AXElement {self.text!r} {self.frame}>"


def class_name(objects: list[Any], obj: Any) -> Optional[str]:
    'Read ``$classname`` of an NSKeyedArchive object.'
    if not isinstance(obj, dict):
        return None
    cls = obj.get("$class")
    index = cls.data if isinstance(cls, plistlib.UID) else None
    if index is not None and isinstance(objects[index], dict):
        name = objects[index].get("$classname")
        return name if isinstance(name, str) else None
    return None


def unwrap_payload(payload: bytes, depth: int = 0) -> bytes:
    'Unwrap ``NSMutableData`` / ``NSData`` to get the real response table.'
    if depth > 4:
        return payload
    try:
        objects = plistlib.loads(payload)["$objects"]
    except Exception:
        return payload
    if any(class_name(objects, obj) == "AXPTranslatorResponse" for obj in objects):
        return payload
    for obj in objects:
        if isinstance(obj, dict) and isinstance(obj.get("NS.data"), bytes):
            return unwrap_payload(obj["NS.data"], depth + 1)
    return payload


class _Archive:
    'Decode the NSKeyedArchive by hand (there is no ready class map for AXPTranslatorResponse).'

    def __init__(self, data: bytes) -> None:
        self.objs: list[Any] = plistlib.loads(data)["$objects"]

    def _class_name(self, obj: dict[str, Any]) -> Optional[str]:
        return class_name(self.objs, obj)

    def value(self, v: Any, depth: int = 0) -> Any:
        if depth > 12:
            return None
        if isinstance(v, plistlib.UID):
            v = self.objs[v.data]
        if isinstance(v, dict):
            name = self._class_name(v)
            if name == "AXPTranslationObject":
                return ("ELEM", self.value(v.get("objectID"), depth + 1),
                        self.value(v.get("pid"), depth + 1))
            if name == "NSValue":
                return self.value(v.get("NS.rectval"), depth + 1)
            if name and "String" in name:
                return self.value(v.get("NS.string") or v.get("NSString"), depth + 1)
            if "Array" in (name or ""):
                return [self.value(i, depth + 1) for i in (v.get("NS.objects") or [])]
            if "Data" in (name or ""):
                return self.value(v.get("NS.data"), depth + 1)
            if name in ("NSDictionary", "NSMutableDictionary"):
                keys = v.get("NS.keys") or []
                vals = v.get("NS.objects") or []
                return {str(self.value(k, depth + 1)): self.value(x, depth + 1)
                        for k, x in zip(keys, vals)}
            return None
        if isinstance(v, (list, tuple)):
            return [self.value(i, depth + 1) for i in v]
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except Exception:
                return f"<data {len(v)}>"
        return v

    def responses(self):
        for obj in self.objs:
            if isinstance(obj, dict) and self._class_name(obj) == "AXPTranslatorResponse":
                yield obj


def parse_payload(payload: bytes) -> tuple[str, list[AXElement]]:
    'Decode one AXPTranslatorResponse payload into ``(dump type, elements)``.'
    archive = _Archive(unwrap_payload(payload))
    return _dump_type(archive), _elements_of(archive)


def _dump_type(archive: _Archive) -> str:
    'The ``treeDumpType`` carried by this payload (empty when it has none).'
    for response in archive.responses():
        data = archive.value(response.get("resultData"))
        if isinstance(data, dict) and "treeDumpType" in data:
            return _text_of(archive.value(data.get("treeDumpType")))
    return ""


def _elements_of(archive: _Archive) -> list[AXElement]:
    'Every element described by the ``attribute == 0`` responses of one archive.'
    elements: dict[int, AXElement] = {}
    for response in archive.responses():
        if str(archive.value(response.get("attribute"))) != "0":
            continue
        handle = archive.value(response.get("associatedTranslationObject"))
        data = archive.value(response.get("resultData"))
        if not (isinstance(handle, tuple) and isinstance(data, dict)):
            continue
        if _capability_table(data):
            continue
        object_id = handle[1]
        if not isinstance(object_id, int):
            continue
        old = elements.get(object_id)
        if old is None:
            elements[object_id] = AXElement(object_id, handle[2], data)
        else:
            merged = _merge_attrs(old.raw, data)
            elements[object_id] = AXElement(object_id, handle[2] or old.pid, merged)
    return list(elements.values())


def parse_elements(payload: bytes) -> list[AXElement]:
    'Decode all elements from one AXPTranslatorResponse payload.'
    return parse_payload(payload)[1]


def _merge_attrs(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    'Merge attribute dicts: ``extra`` overrides ``base``, but keys whose value is ``None`` do not.'
    return {**base, **{key: value for key, value in extra.items()
                       if value is not None}}


def _capability_table(data: dict[str, Any]) -> bool:
    'The "supported attributes" bit table inside AdditionalData: keys are attribute ids and values are all bools, not attributes.'
    return len(data) > 20 and all(isinstance(value, bool) for value in data.values())


class AXClient:
    'Fetch the control tree of the current screen (with coordinates).'

    def __init__(self, rsd: RSD, service_name: str = SERVICE_AX) -> None:
        self.rsd = rsd
        self.service_name = service_name
        self.dtx: Optional[DTXConnection] = None
        self.last_len = 0
        self.stats: dict[str, int] = {"messages": 0, "payloads": 0, "rounds": 0}

    def connect(self) -> "AXClient":
        self.close()
        port = self.rsd.service_port(self.service_name)
        conn = self.rsd.stack.connect(port)
        self.dtx = DTXConnection(conn)
        self.dtx.handshake()
        return self

    def close(self) -> None:
        if self.dtx is not None:
            self.dtx.close()
            self.dtx = None

    def __enter__(self) -> "AXClient":
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def stale(self) -> bool:
        'Whether the latest payload was a truncated dump.'
        return 0 < self.last_len < STALE_DUMP_SIZE

    def fetch(self, timeout: float = 15.0, fresh: bool = True,
              quiet: float = QUIET_WINDOW) -> list[AXElement]:
        'Trigger one dump, collect until it goes quiet, and return the last complete round.'
        if fresh or self.dtx is None:
            self.connect()
        if self.dtx is None:
            raise OpenDeviceError('AX connection was not established (connect() failed)')
        self.dtx.incoming.clear()

        self.dtx.invoke("clientNeedsAccessibility:", (True,), channel_code=0,
                        expects_reply=True, timeout=8.0, ignore_errors=True)

        return self._collect(timeout, quiet)

    def _collect(self, timeout: float, quiet: float) -> list[AXElement]:
        'Collect payloads until the stream goes quiet, keeping only the latest dump round.'
        if self.dtx is None:
            raise OpenDeviceError('AX connection was not established (connect() failed)')
        deadline = time.monotonic() + timeout
        elements: dict[int, AXElement] = {}
        cursor = 0
        last_packet = 0.0
        filled = False                  # the current round already received its AdditionalData
        while time.monotonic() < deadline and (not last_packet
                                          or time.monotonic() - last_packet < quiet):
            while cursor < len(self.dtx.incoming):
                payload = _payload_of(self.dtx.incoming[cursor])
                cursor += 1
                self.stats["messages"] += 1
                if payload is None:
                    continue
                kind, parsed = parse_payload(payload)
                if kind == DUMP_INITIAL and filled:
                    # A fresh snapshot starts: whatever the previous round held was transitional.
                    elements.clear()
                    filled = False
                    self.stats["rounds"] += 1
                if kind == DUMP_ADDITIONAL:
                    filled = True
                self.stats["payloads"] += 1
                self.last_len = len(payload)
                self.stats["last_len"] = len(payload)
                last_packet = time.monotonic()
                for element in parsed:
                    old = elements.get(element.object_id)
                    if old is None:
                        elements[element.object_id] = element
                    else:
                        merged = _merge_attrs(old.raw, element.raw)
                        elements[element.object_id] = AXElement(
                            element.object_id, element.pid or old.pid, merged)
            if last_packet and time.monotonic() - last_packet >= quiet:
                break
            self.dtx.pump(POLL_TICK)
        return list(elements.values())


def _payload_of(message: Any) -> Optional[bytes]:
    'Is this DISPATCH a ``processDataFromRemoteDevice:`` dump? If so, extract the bytes.'
    if message.type != DISPATCH or message.name != "processDataFromRemoteDevice:":
        return None
    for arg in message.aux:
        if isinstance(arg, (bytes, bytearray)) and len(arg) > 1000:
            return bytes(arg)
    return None


def find(elements: list[AXElement], text: str,
         exact: bool = False) -> Optional[AXElement]:
    'Find an element by text (substring by default; returns the first hit that has a frame).'
    for element in elements:
        haystack = element.text
        hit = (haystack == text) if exact else (text in haystack)
        if hit and element.center:
            return element
    return None


def find_all(elements: list[AXElement], text: str) -> list[AXElement]:
    return [el for el in elements if text in el.text and el.center]


#: The button that approves the iOS paste prompt, per device language.
PASTE_ALLOW_LABELS: dict[str, tuple[str, ...]] = {
    "zh-Hans": ("允许粘贴",),
    "zh-Hant": ("允許貼上", "允許粘貼"),
    "en": ("Allow Paste", "Paste"),
    "ja": ("ペーストを許可",),
}


def find_allow_paste(elements: list[AXElement]) -> Optional[AXElement]:
    'The paste prompt button that approves the paste (exact label match, never a refusal).'
    wanted = {label.casefold() for labels in PASTE_ALLOW_LABELS.values() for label in labels}
    for element in elements:
        label = (element.label or "").strip()
        if label and label.casefold() in wanted and element.center:
            return element
    return None


def to_tree(elements: list[AXElement]) -> list[tuple[int, AXElement]]:
    'Flatten into ``[(depth, element)]`` for printing.'
    return [(depth, element) for depth, _last, element in tree_nodes(elements)]


def tree_nodes(elements: list[AXElement]) -> list[tuple[int, bool, AXElement]]:
    '``[(depth, is_last_child_of_parent, element)]``.'
    by_id = {el.object_id: el for el in elements}
    child_ids = {c for el in elements for c in el.children}
    roots = [el for el in elements if el.object_id not in child_ids]
    out: list[tuple[int, bool, AXElement]] = []
    seen: set[int] = set()

    def walk(element: AXElement, depth: int, last: bool) -> None:
        if element.object_id in seen or depth > 20:
            return
        seen.add(element.object_id)
        out.append((depth, last, element))
        kids = [by_id[c] for c in element.children if c in by_id]
        for index, child in enumerate(kids):
            walk(child, depth + 1, index == len(kids) - 1)

    for index, root in enumerate(roots or elements):
        walk(root, 0, index == len(roots or elements) - 1)
    return out


TRAIT_NAMES = [(1, 'Button'), (1024, 'SearchField'), (2, 'Link'), (65536, 'Header'), (64, 'Text'),
               (4, 'Image'), (4096, 'Adjustable'), (8, 'Selected'), (256, 'Disabled')]

TEXT_COLUMNS = 44


def trait_names(element: AXElement) -> list[str]:
    'Which traits this element carries (in the order above).'
    traits = element.traits if isinstance(element.traits, int) else 0
    return [name for bit, name in TRAIT_NAMES if traits & bit]


def format_tree(elements: list[AXElement], limit: int = 40,
                contains: Optional[str] = None) -> str:
    'Render the controls as a tree: tree lines + frame + text + traits.'
    from ..core import console

    nodes = tree_nodes(elements)
    if contains:
        keep = _hit_and_ancestors(nodes, contains)
        nodes = [node for node in nodes if node[2].object_id in keep]
    shown, rest = nodes[:limit], nodes[limit:]
    columns = max((console.width(_frame(node[2])) for node in shown), default=1)

    lines: list[str] = []
    branch: list[bool] = []
    for depth, last, element in shown:
        branch = branch[:depth]
        lines.append(_branch(branch, last) + _cells(element, columns))
        branch.append(last)
    if rest:
        lines.append(f"... {len(rest)} more (--limit {limit} stops here)")
    return "\n".join(lines)


def _branch(ancestors: list[bool], last: bool) -> str:
    'Tree lines: ``|  `` continues, ``+- `` branches, ``\\- `` closes.'
    trunk = "".join("│  " if not done else "   " for done in ancestors)
    return trunk + ("└─ " if last else "├─ ")


def _cells(element: AXElement, columns: int) -> str:
    'Everything after the tree line on one row: frame (aligned) + text + traits.'
    from ..core import console

    text = console.clip(_label(element), TEXT_COLUMNS)
    tags = "·".join(name for name in trait_names(element)
                    if name not in ('Text', 'Image'))
    tail = f"  [{tags}]" if tags else ""
    return f"{console.pad(_frame(element), columns)} {text}{tail}".rstrip()


def _frame(element: AXElement) -> str:
    '``(x,y wxh)``.'
    if not element.frame:
        return "—"
    return (f"({element.x:.0f},{element.y:.0f} "
            f"{element.width:.0f}x{element.height:.0f})")


def _label(element: AXElement) -> str:
    'Display name: text > a user_labels fallback > the kind name (identities like ``<Button>``).'
    text = " ".join((element.text or "").split())
    if not text and element.user_labels:
        text = " ".join(str(element.user_labels[0]).split())
    if text:
        return text
    return f"<{element.kind}>" if element.kind else "·"


def _hit_and_ancestors(nodes: list[tuple[int, bool, AXElement]], text: str) -> set[int]:
    'The matched element **plus every ancestor** (otherwise a filtered tree is not a tree).'
    keep: set[int] = set()
    stack: list[int] = []
    for depth, _last, element in nodes:
        stack = stack[:depth] + [element.object_id]
        if text.lower() in element.text.lower():
            keep.update(stack)
    return keep
