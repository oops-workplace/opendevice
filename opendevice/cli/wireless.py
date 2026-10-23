from __future__ import annotations

import argparse
import time

from ..core import output, paths
from ..transport.lockdown import LockdownClient


def cmd_pairing(args: argparse.Namespace) -> int:
    'Create remote pairing credentials over USB (via a trusted lockdownd; no prompt, no code).'
    from ..wireless.rrp import pair_over_lockdown

    if args.force:
        for path in paths.unpair(all_records=True):
            output.note(f"deleted the old record {path}")

    client = LockdownClient(args.udid, args.mux)
    client.start_session()
    try:
        record = pair_over_lockdown(client, udid=args.udid)
    finally:
        client.close()
    output.line(f"remote pairing established:{record.identifier}")
    output.line(output.pairs([
        ('record written to ', record.path),
        ("altIRK", record.peer_alt_irk.hex() if record.peer_alt_irk else '(empty)'),
    ]))
    output.note('the Wi-Fi link is usable now: python -m opendevice wifi --seconds 8')
    return 0


def cmd_unpairing(args: argparse.Namespace) -> int:
    'Delete local remote pairing records.'
    if args.all:
        removed = paths.unpair(all_records=True)
        for path in removed:
            output.line(f"deleted {path}")
        output.note('All remote pairing records on this host were cleared. The copy on the device stays until the next pairing.'
                    if removed else f"no remote pairing record ({paths.records_dir()})")
        return 0

    identifier = args.udid
    if not identifier:
        known = paths.record_paths(all_records=True)
        if not known:
            output.note(f"no remote pairing record ({paths.records_dir()})")
            return 0
        names = sorted({path.name[len(paths.RECORD_PREFIX):-len(paths.RECORD_SUFFIX)]
                        for path in known})
        if len(names) > 1:
            output.die('several records exist; pick one with -d <UDID>, or --all to delete them all:\n'
                       + "\n  ".join(names))
        identifier = names[0]

    removed = paths.unpair(identifier=identifier)
    if not removed:
        output.note(f"no {identifier} remote pairing record ({paths.records_dir()})")
        return 0
    for path in removed:
        output.line(f"deleted {path}")
    output.note('the local ticket is gone; the device-side one is not - the next pairing runs pair-setup again and overwrites it.')
    return 0


def cmd_wifi(args: argparse.Namespace) -> int:
    from ..remote.rsd import RSD
    from ..wireless.wifi import list_devices, open_wifi_tunnel

    if args.list:
        found = list_devices()
        if not found:
            output.die('no _remotepairing device found (same subnet? device unlocked?)')
        output.line(output.table(
            ["UDID", 'Address', 'Port', 'Version'],
            [[service.identifier, service.best_address(), service.port, service.version]
             for service in found], aligns=("l", "l", "r", "l")))
        return 0

    tunnel, info = open_wifi_tunnel(udid=args.udid, host=args.host,
                                    identifier=args.identifier)
    output.note(f"Wi-Fi tunnel established{info}")
    rsd = RSD(tunnel, info, threaded=True)
    seen = {"n": 0, "key": 0}
    try:
        rsd.connect()
        output.note(f"RSD ready: {len(rsd.services)} services"
                    f"{rsd.product_type} iOS {rsd.product_version}")
        if args.seconds <= 0:
            return 0

        from ..media.stream import MediaStreamSession

        session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
        session.prepare_socket()
        session.start()
        output.note(f"streaming started: {session.describe()}")

        def on_unit(_data: bytes, is_key: bool) -> None:
            seen["n"] += 1
            if is_key:
                seen["key"] += 1

        receiver = session.receiver_for(on_unit)
        receiver.start()
        time.sleep(args.seconds)
        receiver.stop()
        session.stop()
        output.line(output.pairs([
            ('access units', f"{seen['n']} (key frames {seen['key']})"),
            ('lost', receiver.stats.get("lost", 0)),
        ]))
    finally:
        rsd.close()
        tunnel.close()
    return 0 if seen["n"] else 1
