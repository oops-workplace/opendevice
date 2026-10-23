from __future__ import annotations

import plistlib
import socket
import struct
import time

import pytest

from opendevice.ax import (
    AXElement,
    ax as ax_module,
    dtx,
    find,
    find_all,
    format_tree,
    parse_rect,
    to_tree,
    trait_names,
    tree_nodes,
    unwrap_payload,
)
from opendevice.input import hid
from opendevice.media import media, rtp
from opendevice.remote import xpc
from opendevice.remote.ipstack import _udp_ports
from opendevice.remote.tunnel import (
    LOOPBACK_HEADER,
    SocketTunnel,
    TunnelError,
    _info_from_handshake,
    _packet,
    _read_packet,
)
from opendevice.wireless import rrp


def _pair() -> "tuple[socket.socket, socket.socket]":
    'A pair of connected sockets, for testing "send out / read back".'
    left, right = socket.socketpair()
    left.settimeout(2.0)
    right.settimeout(2.0)
    return left, right


def _ipv6(payload: bytes = b"hello") -> bytes:
    'Build a minimal IPv6 packet (40-byte header + payload).'
    header = bytearray(40)
    header[0] = 0x60
    header[4:6] = struct.pack(">H", len(payload))
    header[6] = 17
    header[7] = 64
    return bytes(header) + payload


class _FakeConn:
    'A socket stand-in that hands out prepared chunks and records what was sent.'

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []

    def recv(self, timeout: float) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass


def _ax_payload(items, dump_type=None) -> bytes:
    'Build one AXPTranslatorResponse payload; ``items`` is ``[(object_id, attrs), ...]``.'
    objects = ["$null",
               {"$classname": "AXPTranslatorResponse"},
               {"$classname": "AXPTranslationObject"},
               {"$classname": "NSDictionary"}]
    response_class = plistlib.UID(1)
    element_class = plistlib.UID(2)
    dict_class = plistlib.UID(3)

    def add(obj):
        objects.append(obj)
        return plistlib.UID(len(objects) - 1)

    def dictionary(mapping):
        return add({"$class": dict_class,
                    "NS.keys": [add(key) for key in mapping],
                    "NS.objects": [add(value) for value in mapping.values()]})

    def response(element, data):
        add({"$class": response_class, "attribute": add(0),
             "associatedTranslationObject": element, "resultData": data})

    add(b"x" * 1200)                       # keeps the payload above _payload_of's floor
    for object_id, attrs in items:
        response(add({"$class": element_class, "objectID": object_id, "pid": 42}),
                 dictionary(attrs))
    if dump_type:
        response(None, dictionary({"treeDumpType": dump_type}))
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000,
                           "$top": {"root": plistlib.UID(0)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _dump_message(payload: bytes) -> dtx.DTXMessage:
    'Wrap a payload the way the AX daemon does (``processDataFromRemoteDevice:``).'
    return dtx.DTXMessage(dtx.DISPATCH,
                          aux_data=dtx.build_aux([dtx.PBuf(payload)]),
                          payload_data=dtx.archive_object("processDataFromRemoteDevice:"))


class _FakeDtx:
    'Stands in for ``DTXConnection`` when only ``incoming`` and ``pump`` are used.'

    def __init__(self, payloads: list[bytes]) -> None:
        self.incoming = [_dump_message(payload) for payload in payloads]

    def pump(self, timeout: float = 0.0) -> list[dtx.DTXMessage]:
        time.sleep(0.01)
        return []


class TestDtx:
    def test_aux_matches_captured_bytes(self) -> None:
        'The ``[PInt32(1), "com.apple..."]`` aux header must match a real capture byte for byte.'
        aux = dtx.build_aux([dtx.PInt32(1), "com.apple.accessibility.axAuditDaemon.remoteAXService"])
        captured = ("f001000000000000d9000000000000000a0000000300000001000000"
                    "0a00000002000000c1")
        assert aux.hex().startswith(captured)
        assert len(aux) == 16 + 217

    def test_aux_primitive_round_trip(self) -> None:
        'Only ``P*``-wrapped values are primitives; these must round-trip value by value.'
        args = [dtx.PInt32(7), dtx.PStr("hello"), dtx.PInt64(1 << 40),
                dtx.PBuf(b"\x01\x02"), 1.5]
        parsed = dtx.parse_aux(dtx.build_aux(args))
        assert parsed[0] == 7
        assert parsed[1] == "hello"
        assert parsed[2] == 1 << 40
        assert bytes(parsed[3]) == b"\x01\x02"
        assert parsed[4] == 1.5

    def test_plain_str_and_bool_are_archived(self) -> None:
        'Bare ``str`` / ``bool`` get **archived** (NSKeyedArchive) instead of sent as primitives.'
        parsed = dtx.parse_aux(dtx.build_aux([True, "hi"]))
        assert dtx._unarchive(parsed[0]) is True
        assert dtx._unarchive(parsed[1]) == "hi"

    def test_aux_rejects_raw_bytes(self) -> None:
        'Bytes need ``PBuf``; bare ``bytes`` cannot be archived and raises.'
        with pytest.raises(Exception):
            dtx.build_aux([b"\x01\x02"])

    @pytest.mark.parametrize("obj", [
        "clientNeedsAccessibility:",
        True,
        {"com.apple.private.DTXBlockCompression": 0,
         "com.apple.private.DTXConnection": 1},
    ])
    def test_archive_round_trip(self, obj: object) -> None:
        assert dtx._unarchive(dtx.archive_object(obj)) == obj

    def test_message_fragment_round_trip(self) -> None:
        message = dtx.DTXMessage(
            dtx.DISPATCH, identifier=7, channel_code=0, transport_flags=1,
            aux_data=dtx.build_aux([True]),
            payload_data=dtx.archive_object("clientNeedsAccessibility:"))
        body = message.body()
        stream = dtx._fragment_header(0, 1, len(body), 7, 0, 0, 1) + body
        rest, parsed = dtx._take_message(stream, {})
        assert rest == b""
        assert parsed.type == dtx.DISPATCH
        assert parsed.name == "clientNeedsAccessibility:"
        assert dtx._unarchive(parsed.aux[0]) is True

    def test_pump_skips_garbage(self) -> None:
        'A desynced stream resumes at the next fragment boundary instead of failing the whole fetch.'
        message = dtx.DTXMessage(
            dtx.DISPATCH, identifier=9, channel_code=0, transport_flags=1,
            aux_data=dtx.build_aux([True]),
            payload_data=dtx.archive_object("clientNeedsAccessibility:"))
        body = message.body()
        stream = b"\xde\xad\xbe\xef\x01\x02" + dtx._fragment_header(0, 1, len(body), 9, 0, 0, 1) + body
        client = dtx.DTXConnection(_FakeConn([stream]))     # type: ignore[arg-type]
        messages = client.pump(0.1)
        assert [m.name for m in messages] == ["clientNeedsAccessibility:"]
        assert client.stats["resync"] == 1

    def test_pump_raises_without_a_boundary(self) -> None:
        client = dtx.DTXConnection(_FakeConn([b"\x00" * 40]))   # type: ignore[arg-type]
        with pytest.raises(dtx.FramingError):
            client.pump(0.1)


class TestAx:
    @pytest.mark.parametrize("raw,expected", [
        ("{{12.5, 30.25}, {100, 44.5}}", (12.5, 30.25, 100.0, 44.5)),
        ("{{0, 0}, {1, 2}}", (0.0, 0.0, 1.0, 2.0)),
    ])
    def test_parse_rect(self, raw: str, expected: tuple) -> None:
        assert parse_rect(raw) == expected

    def test_parse_rect_rejects_junk(self) -> None:
        assert parse_rect("nonsense") is None
        assert parse_rect(None) is None

    def test_role_and_placeholder_are_read(self) -> None:
        'Attributes "78" (role description) and "42" (placeholder) cover what the kind table cannot.'
        field = AXElement(1, 1, {"45": 15, "42": 'Search', "78": ['Search Field']})
        assert field.role == 'Search Field'
        assert field.placeholder == 'Search'
        assert field.kind == 'Search Field'
        assert field.text == 'Search'

    def test_role_joins_every_entry(self) -> None:
        '``kind`` falls back to the first entry of ``role``.'
        element = AXElement(2, 1, {"78": ['Button', 'Static Text', 'Switch']})
        assert element.role == 'Button·Static Text·Switch'
        assert element.kind == 'Button'

    def test_known_kind_wins_over_the_role(self) -> None:
        button = AXElement(3, 1, {"45": 2, "78": ['Static Text']})
        assert button.kind == 'Button'

    def test_array_attributes_are_coerced(self) -> None:
        'Some apps deliver text as a one-entry array; every text field must still be a str.'
        element = AXElement(4, 1, {"3": ['Like'], "33": ['Save'], "64": ['347'], "42": []})
        assert element.label == 'Like'
        assert element.description == 'Save'
        assert element.value == '347'
        assert element.placeholder == ''
        assert element.text == 'Like'

    def test_find_and_tree(self) -> None:
        'Elements are built from attribute dicts; parent/child comes from objectIDs in ``ATTR_CHILDREN`` ("9").'
        parent = AXElement(1, 100, {"3": 'search', "21": "{{180, 700}, {42, 42}}",
                                    "9": [("ELEM", 2, 100)]})
        child = AXElement(2, 100, {"3": 'camera', "21": "{{60, 130}, {62, 62}}"})
        elements = [parent, child]

        assert parent.children == [2]
        assert find(elements, 'search') is parent
        assert find(elements, 'missing') is None
        assert [e.label for e in find_all(elements, 'cam')] == ['camera']
        assert [(depth, e.label) for depth, e in to_tree(elements)] == \
            [(0, 'search'), (1, 'camera')]

    def test_tree_knows_who_is_the_last_child(self) -> None:
        '``is_last`` picks ``+- `` or ``\\- ``.'
        root = AXElement(1, 1, {"3": 'root', "9": [("E", 2, 1), ("E", 3, 1)]})
        nodes = tree_nodes([root, AXElement(2, 1, {"3": 'a'}), AXElement(3, 1, {"3": 'b'})])
        assert [(depth, last, e.label) for depth, last, e in nodes] == \
            [(0, True, 'root'), (1, False, 'a'), (1, True, 'b')]

    def test_format_tree_draws_a_tree(self) -> None:
        root = AXElement(1, 1, {"3": 'screen', "21": "{{0, 0}, {100, 200}}",
                                "9": [("E", 2, 1)]})
        kid = AXElement(2, 1, {"3": 'button a', "21": "{{1, 2}, {3, 4}}", "77": 1})
        assert format_tree([root, kid]).splitlines() == [
            '└─ (0,0 100x200) screen',
            '   └─ (1,2 3x4)     button a  [Button]',
        ]

    def test_format_tree_filter_keeps_ancestors(self) -> None:
        'Filtering to only the hit would float it to the root - keep the ancestors so its place is visible.'
        root = AXElement(1, 1, {"3": 'root', "9": [("E", 2, 1)]})
        mid = AXElement(2, 1, {"3": 'mid', "9": [("E", 3, 1)]})
        leaf = AXElement(3, 1, {"3": 'leaf'})
        lines = format_tree([root, mid, leaf], contains='leaf').splitlines()
        assert len(lines) == 3 and 'root' in lines[0] and 'leaf' in lines[2]

    def test_trait_names(self) -> None:
        assert trait_names(AXElement(1, 1, {"77": 1024 | 8})) == ['SearchField', 'Selected']
        assert trait_names(AXElement(1, 1, {})) == []

    def test_unwrap_nested_data(self) -> None:
        'Device payloads are nested: the outer one holds a single NSMutableData; the real table is in ``NS.data``.'
        inner = plistlib.dumps({"$objects": [{"$classname": "AXPTranslatorResponse"}],
                                "$archiver": "NSKeyedArchiver"})
        outer = plistlib.dumps({
            "$objects": ["$null", {"NS.data": inner}],
            "$archiver": "NSKeyedArchiver",
        })
        assert unwrap_payload(outer) == inner
        assert unwrap_payload(inner) == inner

    def test_parse_payload_reports_the_dump_type(self) -> None:
        payload = _ax_payload([(7, {"3": "Hello"})], dump_type=ax_module.DUMP_INITIAL)
        kind, elements = ax_module.parse_payload(payload)
        assert kind == ax_module.DUMP_INITIAL
        assert [el.label for el in elements] == ["Hello"]

    def test_collect_drops_the_transitional_round(self) -> None:
        'A dump that restarts (``InitialDump`` after ``AdditionalData``) discards the earlier round.'
        client = ax_module.AXClient(None)                 # type: ignore[arg-type]
        client.dtx = _FakeDtx([                            # type: ignore[assignment]
            _ax_payload([(1, {"3": "stale"})], ax_module.DUMP_INITIAL),
            _ax_payload([(1, {"3": "stale"})], ax_module.DUMP_ADDITIONAL),
            _ax_payload([(2, {"3": "fresh"})], ax_module.DUMP_INITIAL),
            _ax_payload([(2, {"3": "fresh"})], ax_module.DUMP_ADDITIONAL),
        ])
        assert [el.label for el in client._collect(timeout=0.5, quiet=0.05)] == ["fresh"]
        assert client.stats["rounds"] == 1

    def test_collect_merges_back_to_back_initial_dumps(self) -> None:
        'Consecutive ``InitialDump`` payloads belong to the same round until it is filled.'
        client = ax_module.AXClient(None)                  # type: ignore[arg-type]
        client.dtx = _FakeDtx([                            # type: ignore[assignment]
            _ax_payload([(1, {"3": "a"})], ax_module.DUMP_INITIAL),
            _ax_payload([(2, {"3": "b"})], ax_module.DUMP_INITIAL),
            _ax_payload([(2, {"3": "b"})], ax_module.DUMP_ADDITIONAL),
        ])
        elements = client._collect(timeout=0.5, quiet=0.05)
        assert sorted(el.label for el in elements) == ["a", "b"]
        assert client.stats["rounds"] == 0


class TestHid:
    def test_report_sizes(self) -> None:
        'A wrong size makes the device drop packets silently (without dropping the connection), so pin it.'
        assert len(hid.touch_report(0.5, 0.5)) == 58
        assert len(hid.pointer_report(0.5, 0.5)) == 19
        assert len(hid.keyboard_report([0x04])) == 39

    def test_touch_round_trip(self) -> None:
        parsed = hid.parse_touch_report(hid.touch_report(0.25, 0.75, down=False))
        assert parsed["down"] is False
        assert parsed["x"] == pytest.approx(0.25, abs=1e-3)
        assert parsed["y"] == pytest.approx(0.75, abs=1e-3)

    def test_touch_down_flag(self) -> None:
        assert hid.parse_touch_report(hid.touch_report(0.1, 0.1, down=True))["down"] is True

    def test_timestamp_is_mach_time_not_nanoseconds(self) -> None:
        'Timestamps must be **Mach ticks (24 MHz)**, not nanoseconds - a factor of 41.67.'
        start = time.monotonic()
        first = hid.pointer_report(0.1, 0.1)
        time.sleep(0.05)
        second = hid.pointer_report(0.1, 0.1)
        delta = (int.from_bytes(second[11:17], "little")
                 - int.from_bytes(first[11:17], "little"))
        ticks_per_second = delta / (time.monotonic() - start)
        assert 20e6 < ticks_per_second < 28e6, (
            f"wrong clock scale: {ticks_per_second/1e6:.1f} MHz (captures use 24)")

    @pytest.mark.parametrize("char", ["A", "z", "1", "!", " ", "/", ":"])
    def test_ascii_map_covers(self, char: str) -> None:
        usage, shift = hid.ASCII_TO_HID[char]
        assert 0 < usage < 0x100
        assert isinstance(shift, bool)

    @staticmethod
    def _hid_double() -> "hid.UniversalHID":
        'No real connection: only capture the reports about to be sent.'
        client = hid.UniversalHID.__new__(hid.UniversalHID)
        client.cursor_min_interval = 0.0
        client._last_cursor_at = 0.0
        return client

    def test_move_cursor_goes_to_the_gesture_surface(self) -> None:
        'The on-device grey dot is driven by 1281 (touchscreenGesture), not by the touch screen 257.'
        client = self._hid_double()
        seen: dict = {}
        client.send_once = lambda report, handle=None: seen.update(  # type: ignore[method-assign]
            report=report, handle=handle) or True
        assert client.move_cursor(0.25, 0.75) is True
        assert seen["handle"] == hid.HANDLE_GESTURE_CURSOR == 1281
        assert len(seen["report"]) == 19 and seen["report"][0] == 0x13
        x = struct.unpack("<i", seen["report"][1:5])[0]
        y = struct.unpack("<i", seen["report"][5:9])[0]
        assert x == pytest.approx(0.25 * 65536, abs=2)
        assert y == pytest.approx(0.75 * 65536, abs=2)

    def test_move_cursor_is_throttled(self) -> None:
        'A mouse can reach 120 Hz; sending every sample wastes the connection shared with touch.'
        client = self._hid_double()
        client.cursor_min_interval = 0.05
        sent: list = []
        client.send_once = lambda report, handle=None: sent.append(handle) or True  # type: ignore[method-assign]
        assert client.move_cursor(0.1, 0.1) is True
        assert client.move_cursor(0.2, 0.2) is False
        time.sleep(0.09)
        assert client.move_cursor(0.3, 0.3) is True
        assert len(sent) == 2

    def test_move_cursor_never_breaks_the_connection(self) -> None:
        'Cursor data is optional: skip it when a send fails; a reconnect would cancel an in-flight touch.'
        client = self._hid_double()
        client.send_once = lambda report, handle=None: False   # type: ignore[method-assign]
        assert client.move_cursor(0.5, 0.5) is False

    def test_construction_really_runs(self) -> None:
        'Constructing an HID service must work - do not test ``__new__`` only.'
        client = hid.UniversalHID(rsd=object())    # type: ignore[arg-type]
        assert client._io_lock is not None
        assert client.xpc is None

    @pytest.mark.parametrize("x,y,want_x,want_y", [
        (-0.5, 0.5, 0.0, 0.5),
        (1.7, 0.2, 1.0, 0.2),
        (float("nan"), 0.3, 0.0, 0.3),
    ])
    def test_move_cursor_clamps_out_of_range(self, x: float, y: float,
                                             want_x: float, want_y: float) -> None:
        client = self._hid_double()
        seen: dict = {}
        client.send_once = lambda report, handle=None: seen.update(  # type: ignore[method-assign]
            report=report) or True
        client.move_cursor(x, y)
        got_x = struct.unpack("<i", seen["report"][1:5])[0]
        got_y = struct.unpack("<i", seen["report"][5:9])[0]
        assert got_x == pytest.approx(want_x * 65536, abs=2)
        assert got_y == pytest.approx(want_y * 65536, abs=2)


class TestRrp:
    def test_tlv_round_trip(self) -> None:
        tlv = rrp.build_tlv([(rrp.TLV.STATE, b"\x01"),
                             (rrp.TLV.PUBLIC_KEY, bytes(range(32)))])
        parsed = rrp.parse_tlv(tlv)
        assert parsed[rrp.TLV.STATE] == b"\x01"
        assert parsed[rrp.TLV.PUBLIC_KEY] == bytes(range(32))

    def test_tlv_splits_long_values(self) -> None:
        'Over 255 bytes must be chunked and joined back on decode (the 384-byte SRP public key takes this path).'
        value = bytes(range(256)) * 2
        tlv = rrp.build_tlv([(rrp.TLV.PUBLIC_KEY, value)])
        assert tlv[1] == 255
        assert rrp.parse_tlv(tlv)[rrp.TLV.PUBLIC_KEY] == value

    def test_empty_value(self) -> None:
        assert rrp.parse_tlv(rrp.build_tlv([(rrp.TLV.STATE, b"")]))[rrp.TLV.STATE] == b""

    def test_frame_round_trip(self) -> None:
        left, right = _pair()
        try:
            rrp.send_frame(left, {"hello": 'world', "n": 1})
            assert rrp.recv_frame(right) == {"hello": 'world', "n": 1}
        finally:
            left.close()
            right.close()

    def test_frame_rejects_wrong_magic(self) -> None:
        left, right = _pair()
        try:
            left.sendall(b"NOTAHAHA" + struct.pack(">H", 2) + b"{}")
            with pytest.raises(Exception):
                rrp.recv_frame(right)
        finally:
            left.close()
            right.close()

    def test_bytes_become_base64(self) -> None:
        'JSON cannot hold bytes - turn them into base64 strings the way Apple does.'
        left, right = _pair()
        try:
            rrp.send_frame(left, {"key": b"\x00\xff"})
            assert rrp.recv_frame(right) == {"key": "AP8="}
        finally:
            left.close()
            right.close()

    def test_hkdf_is_deterministic(self) -> None:
        assert rrp._hkdf(b"ikm", None, b"ClientEncrypt-main") == \
            rrp._hkdf(b"ikm", None, b"ClientEncrypt-main")
        assert len(rrp._hkdf(b"ikm", None, b"info")) == 32
        assert rrp._hkdf(b"ikm", None, b"info") != rrp._hkdf(b"ikm", None, b"other")

    def test_hkdf_empty_salt_equals_no_salt(self) -> None:
        'In HKDF "no salt" means an all-zero salt, so ``None`` and ``b""`` are equal - noted so nobody trips.'
        assert rrp._hkdf(b"ikm", None, b"info") == rrp._hkdf(b"ikm", b"", b"info")

    def test_record_filename_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("OPENDEVICE_HOME", str(tmp_path))
        record = rrp.RemotePairRecord(identifier="EXAMPLE-0001", public_key=b"p" * 32,
                                      private_key=b"s" * 32, peer_alt_irk=b"i" * 16)
        record.save()
        assert (tmp_path / "remote_EXAMPLE-0001.plist").is_file()
        loaded = rrp.RemotePairRecord.load("EXAMPLE-0001")
        assert loaded is not None
        assert loaded.public_key == b"p" * 32
        assert loaded.peer_alt_irk == b"i" * 16

    def test_best_address_prefers_ipv4(self) -> None:
        service = rrp.RemoteService(identifier="x", host="dev.local", port=1,
                                    addresses=["fe80::1", "192.168.1.5"])
        assert service.best_address() == "192.168.1.5"
        assert rrp.RemoteService(identifier="x", host="dev.local", port=1,
                                 addresses=["fe80::1"]).best_address() == "fe80::1"


class TestTunnel:
    def test_cdtunnel_packet_round_trip(self) -> None:
        left, right = _pair()
        try:
            left.sendall(_packet({"type": "clientHandshakeRequest", "mtu": 16000}))
            assert _read_packet(right) == b'{"type": "clientHandshakeRequest", "mtu": 16000}'
        finally:
            left.close()
            right.close()

    def test_recv_packet_plain(self) -> None:
        packet = _ipv6(b"payload")
        left, right = _pair()
        try:
            tunnel = SocketTunnel()
            tunnel._sock = right
            left.sendall(packet)
            assert tunnel.recv_packet() == packet
        finally:
            left.close()
            right.close()

    def test_recv_packet_strips_loopback_header(self) -> None:
        'On Windows inbound packets carry 4 extra bytes ``00 00 86 dd``.'
        packet = _ipv6(b"payload")
        left, right = _pair()
        try:
            tunnel = SocketTunnel()
            tunnel._sock = right
            left.sendall(LOOPBACK_HEADER + packet)
            assert tunnel.recv_packet() == packet
        finally:
            left.close()
            right.close()

    def test_recv_packet_rejects_non_ipv6(self) -> None:
        left, right = _pair()
        try:
            tunnel = SocketTunnel()
            tunnel._sock = right
            left.sendall(b"\x45\x00\x00\x14" + b"\x00" * 32)
            with pytest.raises(TunnelError):
                tunnel.recv_packet()
        finally:
            left.close()
            right.close()

    def test_handshake_response_parsing(self) -> None:
        info = _info_from_handshake({
            "serverAddress": "fd81::1", "serverRSDPort": 65438,
            "clientParameters": {"address": "fd81::2", "mtu": 16000},
        })
        assert info.device_address == "fd81::1"
        assert info.host_address == "fd81::2"
        assert info.rsd_port == 65438
        assert info.mtu == 16000

    def test_handshake_response_requires_fields(self) -> None:
        with pytest.raises(TunnelError):
            _info_from_handshake({"clientParameters": {"address": ""}})


def _rtp_packet(payload: bytes, seq: int = 1, marker: bool = False,
                payload_type: int = 100) -> bytes:
    header = struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | payload_type,
                         seq, 90000 * seq, 0xDEADBEEF)
    return header + payload


class TestRtp:
    def test_parse_rtp(self) -> None:
        info = rtp.parse_rtp(_rtp_packet(b"\x01\x02\x03", seq=42))
        assert info is not None
        assert info["seq"] == 42
        assert info["ssrc"] == 0xDEADBEEF
        assert info["payload"] == b"\x01\x02\x03"

    def test_parse_rtp_rejects_short(self) -> None:
        'Only the length is checked (the version field is not validated).'
        assert rtp.parse_rtp(b"\x00" * 8) is None
        assert rtp.parse_rtp(b"\x00" * 11) is None
        assert rtp.parse_rtp(b"\x00" * 12) is not None

    def test_parse_rtp_rejects_overrunning_extension(self) -> None:
        'An extension header that does not fit -> None.'
        packet = struct.pack(">BBHII", 0x90, 100, 1, 0, 0) + struct.pack(">HH", 0xBEDE, 99)
        assert rtp.parse_rtp(packet) is None

    def test_single_nal_becomes_annex_b(self) -> None:
        depacketizer = rtp.Depacketizer()
        au = depacketizer.feed(b"\x26\x01" + b"\xaa" * 8)
        assert au.startswith(rtp.START_CODE)
        assert rtp.START_CODE + b"\x26\x01" + b"\xaa" * 8 == au

    def test_parameter_sets_and_idr_gate_ready(self) -> None:
        depacketizer = rtp.Depacketizer()
        assert not depacketizer.ready()
        depacketizer.feed(b"\x40\x01" + b"\x00" * 4)
        assert not depacketizer.ready()
        depacketizer.feed(b"\x26\x01" + b"\x00" * 4)
        assert depacketizer.ready()
        assert depacketizer.take_au_key() is True
        assert depacketizer.take_au_key() is False

    def test_fragmentation_unit_reassembly(self) -> None:
        'FU (type 49) must be reassembled into one NAL using the start/end bits.'
        depacketizer = rtp.Depacketizer()
        depacketizer.feed(b"\x42\x01" + b"\x00" * 4)
        first = bytes([49 << 1, 0x01]) + b"\x93" + b"\xaa" * 4
        second = bytes([49 << 1, 0x01]) + b"\x53" + b"\xbb" * 4
        assert depacketizer.feed(first) == b""
        au = depacketizer.feed(second)
        assert au == rtp.START_CODE + b"\x26\x01" + b"\xaa" * 4 + b"\xbb" * 4

    def test_invalidate_resets(self) -> None:
        depacketizer = rtp.Depacketizer()
        depacketizer.feed(b"\x40\x01" + b"\x00" * 4)
        depacketizer.feed(b"\x26\x01" + b"\x00" * 4)
        assert depacketizer.ready()
        depacketizer.invalidate()
        assert not depacketizer.ready()
        assert depacketizer.waiting_for_idr() is False

    def test_send_pli(self) -> None:
        left, right = _pair()
        try:
            assert rtp.send_pli(left, ("127.0.0.1", 5000), 0x1111, 0x2222) is True
            packet = right.recv(1024)
            assert packet[:2] == bytes([0x81, rtp.RTCP_PLI])
            assert len(packet) == 12
            assert rtp.send_pli(left, ("127.0.0.1", 5000), 0, 0x2222) is False
        finally:
            left.close()
            right.close()


class TestXpc:
    def test_wrap_layout(self) -> None:
        'Envelope = ``<IIQ>(magic, flags, len)`` + ``<Q>msg_id`` + ``<II>(payload_magic, version)``.'
        blob = xpc.wrap({"hello": 1}, message_id=9, wanting_reply=True)
        magic, flags, length = struct.unpack_from("<IIQ", blob, 0)
        assert magic == xpc.MAGIC_WRAPPER
        assert flags & xpc.FLAG_DATA_PRESENT
        assert flags & xpc.FLAG_WANTING_REPLY
        assert struct.unpack_from("<Q", blob, 16)[0] == 9
        assert struct.unpack_from("<I", blob, 24)[0] == xpc.MAGIC_PAYLOAD
        assert struct.unpack_from("<I", blob, 28)[0] == xpc.PROTOCOL_VERSION
        assert length == len(blob) - 24

    def test_wrap_without_payload_has_no_payload_magic(self) -> None:
        blob = xpc.wrap({})
        flags = struct.unpack_from("<I", blob, 4)[0]
        assert not flags & xpc.FLAG_DATA_PRESENT
        assert len(blob) == 24

    def test_type_markers_differ(self) -> None:
        assert xpc.T_STRING != xpc.T_INT64 != xpc.T_DICT
        assert xpc.XpcUInt64Type(1) == 1


class TestMedia:
    @pytest.mark.parametrize("value,expected", [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
    ])
    def test_varint(self, value: int, expected: bytes) -> None:
        assert media._varint(value) == expected


class TestIpStack:
    def test_udp_ports(self) -> None:
        udp = struct.pack(">HHHH", 5000, 6000, 8, 0)
        packet = _ipv6(udp)
        assert _udp_ports(packet) == (5000, 6000)

    def test_udp_ports_rejects_non_udp(self) -> None:
        icmp = bytearray(_ipv6(b"\x00" * 8))
        icmp[6] = 58
        assert _udp_ports(bytes(icmp)) is None
