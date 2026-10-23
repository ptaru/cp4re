#!/usr/bin/env python3
"""Wake an EZVIZ CP4 camera over the LAN and/or through the EZVIZ cloud.

LAN path (proven on CP4):
    One ordinary UDP datagram to port 50161 carrying the seven-byte EZLink
    wake header. No account, cloud connection, replacement bootstrap server,
    or raw socket is used.

Cloud path (proven fallback):
    Account login, then the cloud live-view (VTM) handshake. The handshake
    alone woke the tested CP4 in about five seconds, even when it was closed
    immediately after StreamInfoRsp. The EnergySaving/SpeedUpListeningFrequency
    IoT action is also available, but it was not sufficient by itself in the
    first controlled test; prefer the preview handshake.

No serial, host, account, password, or token is stored. Pass the serial and
host explicitly. Credentials are read from EZVIZ_PASSWORD or a hidden prompt.

Examples:
    # LAN only
    python tools/wake.py --host 192.168.1.50 --method lan

    # LAN first, cloud preview as fallback
    python tools/wake.py --host 192.168.1.50 --serial ABC12345 \\
        --account you@example.com --method auto

    # Cloud only, no LAN verification
    python tools/wake.py --serial ABC12345 --account you@example.com \\
        --method cloud
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import time
from pathlib import Path

DEFAULT_LAN_PORT = 50161
DEFAULT_PAYLOAD = "50030000bd0c2f"
DEFAULT_SSH_PORT = 2222
DEFAULT_SHELL_PORT = 2323
DEFAULT_API_URL = "apiieu.ezvizlife.com"


def read_password(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if sys.stdin.isatty():
        try:
            return getpass.getpass("EZVIZ account password: ")
        except (EOFError, OSError) as error:
            raise SystemExit(f"password prompt failed: {error}") from error
    raise SystemExit(
        f"no terminal for a password prompt; set {env_name} or run interactively"
    )


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ports(host: str, ports: list[int], timeout: float = 0.75) -> dict[int, bool]:
    return {port: port_open(host, port, timeout) for port in ports}


def wait_for_ports(host: str, ports: list[int], seconds: float) -> tuple[bool, dict[int, bool], float]:
    started = time.monotonic()
    state = {port: False for port in ports}
    if seconds <= 0:
        return False, state, 0.0
    deadline = started + seconds
    while time.monotonic() < deadline:
        state = check_ports(host, ports)
        if any(state.values()):
            return True, state, round(time.monotonic() - started, 2)
        time.sleep(0.25)
    return False, state, round(time.monotonic() - started, 2)


def lan_wake(host: str, port: int, payload: bytes, source_port: int) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind(("", source_port))
        sender.sendto(payload, (host, port))
        return {
            "host": host,
            "port": port,
            "source_port": sender.getsockname()[1],
            "payload_hex": payload.hex(),
            "sent": True,
        }


def cloud_wake(client, serial: str, mode: str, preview_timeout: float) -> dict:
    """Run the requested cloud trigger(s). Returns a credential-free report."""
    result: dict = {"mode": mode}
    if mode in ("action", "both"):
        try:
            response = client.set_iot_action(
                serial,
                "Video",
                "1",
                "EnergySaving",
                "SpeedUpListeningFrequency",
                {"action": 2, "duration": 60},
            )
            result["action"] = {
                "ok": True,
                "meta": response.get("meta") if isinstance(response, dict) else None,
            }
        except Exception as error:  # noqa: BLE001 - report provider errors as data
            result["action"] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    if mode in ("preview", "both"):
        try:
            from pyezvizapi.cloud_stream import open_cloud_stream

            with open_cloud_stream(client, serial, timeout=preview_timeout) as stream:
                info = stream.start()
            result["preview"] = {
                "ok": True,
                "result": getattr(info, "result", None),
                "has_stream_url": bool(getattr(info, "streamurl", None)),
                "has_session": bool(getattr(info, "streamssn", None)),
            }
        except Exception as error:  # noqa: BLE001
            result["preview"] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", help="camera LAN address (required for LAN wake)")
    parser.add_argument("--serial", help="camera serial (required for cloud wake)")
    parser.add_argument("--account", help="EZVIZ account email (required for cloud wake)")
    parser.add_argument("--password-env", default="EZVIZ_PASSWORD",
                        help="environment variable holding the account password")
    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help="EZVIZ API region host (default: EU)")
    parser.add_argument("--method", choices=["auto", "lan", "cloud"], default="auto")
    parser.add_argument("--cloud-mode", choices=["preview", "action", "both"],
                        default="preview")
    parser.add_argument("--lan-port", type=int, default=DEFAULT_LAN_PORT)
    parser.add_argument("--payload", default=DEFAULT_PAYLOAD,
                        help="wake payload as hex (default: the CP4 EZLink header)")
    parser.add_argument("--source-port", type=int, default=0)
    parser.add_argument("--check-port", action="append", type=int, default=None,
                        help="TCP port that indicates the camera is awake; may repeat")
    parser.add_argument("--wait", type=float, default=30.0,
                        help="seconds to wait for a check port after each trigger")
    parser.add_argument("--preview-timeout", type=float, default=15.0)
    parser.add_argument("--force", action="store_true",
                        help="run even if a check port is already open")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without sending anything")
    parser.add_argument("--json-out", help="write the report here (mode 0600)")
    args = parser.parse_args()

    if args.method == "lan" and not args.host:
        parser.error("--method lan requires --host")
    if args.method == "cloud" and not (args.account and args.serial):
        parser.error("--method cloud requires --account and --serial")
    if args.method == "auto" and not args.host and not (args.account and args.serial):
        parser.error("auto needs --host (LAN) or --account/--serial (cloud)")
    if not 1 <= args.lan_port <= 65535 or not 0 <= args.source_port <= 65535:
        parser.error("invalid port")
    if not 0 <= args.wait <= 180 or not 1 <= args.preview_timeout <= 60:
        parser.error("--wait must be 0..180 and --preview-timeout 1..60")
    try:
        payload = bytes.fromhex(args.payload.replace(" ", ""))
    except ValueError:
        parser.error("--payload must be hex")
    if not 1 <= len(payload) <= 1400:
        parser.error("payload must be 1..1400 bytes")

    check_ports_list = args.check_port or [DEFAULT_SSH_PORT, DEFAULT_SHELL_PORT]
    report: dict = {
        "host": args.host,
        "serial": args.serial,
        "method_requested": args.method,
        "check_ports": check_ports_list,
        "lan": None,
        "cloud": None,
        "verified": bool(args.host and check_ports_list and args.wait > 0),
    }

    if args.dry_run:
        print(json.dumps({
            "lan": {
                "would_send": bool(args.host and args.method in ("auto", "lan")),
                "target": f"{args.host}:{args.lan_port}" if args.host else None,
                "payload_hex": payload.hex(),
            },
            "cloud": {
                "would_run": bool(args.account and args.serial
                                  and args.method in ("auto", "cloud")),
                "serial": args.serial,
                "mode": args.cloud_mode,
            },
            "check_ports": check_ports_list,
        }, indent=2))
        return 0

    if args.host:
        already = check_ports(args.host, check_ports_list)
        report["already_awake"] = any(already.values())
        if report["already_awake"] and not args.force:
            report["ports"] = already
            report["woke"] = True
            report["via"] = "already-awake"
            print(json.dumps(report, indent=2))
            return 0

    woke = False
    via = None
    started = time.monotonic()

    if args.host and args.method in ("auto", "lan"):
        report["lan"] = lan_wake(args.host, args.lan_port, payload, args.source_port)
        print(f"sent LAN wake to {args.host}:{args.lan_port}", file=sys.stderr, flush=True)
        if args.wait > 0:
            woke, state, elapsed = wait_for_ports(args.host, check_ports_list, args.wait)
            report["lan"]["ports"] = state
            report["lan"]["seconds"] = elapsed
            if woke:
                via = "lan"
                print(f"camera reachable after {elapsed}s", file=sys.stderr, flush=True)
        elif args.method == "lan" or (args.method == "auto" and args.wait <= 0):
            # No verification requested; sending the datagram is the operation.
            via = "lan"
            woke = True

    if not woke and args.method in ("auto", "cloud"):
        if not (args.account and args.serial):
            print(
                "LAN wake did not verify and cloud credentials were not supplied",
                file=sys.stderr,
            )
            report["woke"] = False
            print(json.dumps(report, indent=2))
            return 1
        try:
            from pyezvizapi.client import EzvizClient
        except ImportError:
            print(
                "error: pyezvizapi is required for cloud wake. Install it with:\n"
                "  python -m pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 2
        try:
            client = EzvizClient(
                account=args.account,
                password=read_password(args.password_env),
                url=args.api_url,
                timeout=20,
            )
            client.login()
        except Exception as error:  # noqa: BLE001
            print(f"error: cloud login failed: {error}", file=sys.stderr)
            report["cloud"] = {"login": False, "error": str(error)}
            print(json.dumps(report, indent=2))
            return 1
        print("cloud login OK", file=sys.stderr, flush=True)
        report["cloud"] = {"login": True}
        report["cloud"].update(
            cloud_wake(client, args.serial, args.cloud_mode, args.preview_timeout)
        )
        triggered = any(
            item.get("ok")
            for item in (report["cloud"].get("preview"), report["cloud"].get("action"))
            if isinstance(item, dict)
        )
        if args.host and args.wait > 0:
            woke, state, elapsed = wait_for_ports(args.host, check_ports_list, args.wait)
            report["cloud"]["ports"] = state
            report["cloud"]["seconds"] = elapsed
            if woke:
                via = "cloud"
                print(f"camera reachable after {elapsed}s", file=sys.stderr, flush=True)
        elif triggered:
            # No LAN address to verify against, but the cloud accepted a trigger.
            via = "cloud"
            woke = True
            report["verified"] = False

    report["woke"] = bool(woke)
    report["via"] = via
    report["seconds_total"] = round(time.monotonic() - started, 2)
    if args.host:
        report["ports"] = check_ports(args.host, check_ports_list, timeout=0.75)

    text = json.dumps(report, indent=2) + "\n"
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        target.chmod(0o600)
    print(text, end="")
    return 0 if woke else 1


if __name__ == "__main__":
    raise SystemExit(main())
