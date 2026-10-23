#!/usr/bin/env python3
"""Probe a CP4 for the TCP services observed in its documented states.

This is a read-only TCP connect scan of the host you name. It sends no wake
packet and changes nothing.

Interpretation for the tested CP4:
  2222 open   custom app0 image with key-only TinySSH, Linux awake
  2323 open   historical raw shell image (unauthenticated; LAN only)
  8000 open   experimental in-RAM HCNetSDK listener
  554  open   should not happen on a stock CP4
  none open   Linux asleep, stock firmware, or a firewall is dropping probes

Stock CP4 firmware exposes no LAN media service. A sleeping camera can leave
its Wi-Fi MCU connected, so "no TCP services" does not mean the whole device
is powered off.

Example:
    python tools/probe.py --host 192.168.1.50
    python tools/probe.py --host 192.168.1.50 --port 2222 --port 8000 --json
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

DEFAULT_PORTS = [2222, 2323, 8000, 554]


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", action="append", type=int, default=None,
                        help=f"TCP port to test (default: {DEFAULT_PORTS})")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ports = args.port or DEFAULT_PORTS
    if not 1 <= args.timeout <= 10:
        parser.error("--timeout must be 1..10 seconds")
    results = {port: port_open(args.host, port, args.timeout) for port in ports}
    report = {
        "host": args.host,
        "timeout": args.timeout,
        "ports": results,
        "open": [port for port, opened in results.items() if opened],
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"host {args.host}")
    for port, opened in results.items():
        print(f"  {port:5d} {'open' if opened else 'closed'}")
    if not report["open"]:
        print("no tested TCP service answered: asleep, stock firmware, or filtered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
