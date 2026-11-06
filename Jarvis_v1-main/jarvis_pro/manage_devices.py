"""Register the devices that may use JARVIS through the JARVIS Device app.

    python manage_devices.py add "My laptop" --role owner
    python manage_devices.py add "Ravi's PC" --role guest --server https://jarvis.example.com
    python manage_devices.py list
    python manage_devices.py disable <id>   |   enable <id>   |   remove <id>

`add` prints the device's secret key ONCE, plus a ready-made device.yaml to copy to that device.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import devices  # noqa: E402

DEVICE_YAML = """# JARVIS Device app config for: {name}  (role: {role})
server_url: "{server}"
device_key: "{key}"
"""


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="register a new device")
    a.add_argument("name")
    a.add_argument("--role", choices=devices.ROLES, default="guest")
    a.add_argument("--server", default="http://127.0.0.1:8000", help="URL the device uses to reach server.py")
    sub.add_parser("list", help="list devices")
    for cmd in ("remove", "disable", "enable"):
        sub.add_parser(cmd).add_argument("id")
    args = p.parse_args()

    if args.cmd == "add":
        device, key = devices.add(args.name, args.role)
        print(f"Registered '{device['name']}' as {device['role']} (id: {device['id']}).\n")
        print("Put this in device.yaml next to the JARVIS Device app on THAT device.")
        print("The key is shown only once - anyone with it can connect as this device.\n")
        print(DEVICE_YAML.format(name=device["name"], role=device["role"], server=args.server, key=key))
    elif args.cmd == "list":
        rows = devices.all_devices()
        if not rows:
            print("No devices registered.")
        for d in rows:
            state = "enabled" if d.get("enabled", True) else "DISABLED"
            print(f"{d['id']:<32} {d['role']:<6} {state:<9} last seen {_when(d.get('last_seen')):<17} {d['name']}")
    elif args.cmd == "remove":
        print("Removed." if devices.remove(args.id) else "No device with that id.")
    else:
        ok = devices.set_enabled(args.id, args.cmd == "enable")
        print(f"{args.cmd.capitalize()}d." if ok else "No device with that id.")


if __name__ == "__main__":
    main()
