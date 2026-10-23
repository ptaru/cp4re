#!/usr/bin/env python3
"""Extract a device identity from a legacy EZVIZ bootstrap capture.

The channel-99 (TCP 8666) long-link handshake carries the 32-byte device
identity in the clear in AUTH-III (command 3) and refresh (command 7). The
get_firmware.py tool needs that identity for existing-device authentication.

Accepted input:
  * a classic libpcap capture taken while the device authenticated
  * a raw reassembled long-link byte stream (one direction), with --raw

Capture recipe (router or span port, no credentials needed):
    sudo tcpdump -i <lan-if> -w ezviz-boot.pcap 'host <camera-ip> and tcp port 8666'
    # power-cycle or re-dock the camera while the capture runs

pcapng is not supported. Convert it first:
    editcap -F pcap capture.pcapng capture.pcap

The identity is not a password, but it is a stable device identifier. Treat
captures and extracted identities as private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
from pathlib import Path

MAX_FRAME = 65536
MAX_LENGTH_BYTES = 4
PORT = 8666

PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": "<",
    b"\xa1\xb2\xc3\xd4": ">",
    b"\x4d\x3c\xb2\xa1": "<",
    b"\xa1\xb2\x3c\x4d": ">",
}


class CaptureError(RuntimeError):
    pass


def _payload_from_frame(linktype: int, frame: bytes) -> bytes | None:
    """Return the IPv4 packet from a link-layer frame, or None."""
    if linktype == 1:  # Ethernet
        if len(frame) < 14:
            return None
        offset = 12
        ethertype = int.from_bytes(frame[offset:offset + 2], "big")
        offset = 14
        while ethertype == 0x8100:  # VLAN tag(s)
            if len(frame) < offset + 4:
                return None
            ethertype = int.from_bytes(frame[offset + 2:offset + 4], "big")
            offset += 4
        if ethertype != 0x0800:
            return None
        return frame[offset:]
    if linktype == 113:  # Linux cooked capture (SLL)
        if len(frame) < 16:
            return None
        if int.from_bytes(frame[14:16], "big") != 0x0800:
            return None
        return frame[16:]
    if linktype == 276:  # Linux cooked capture v2 (SLL2)
        if len(frame) < 20:
            return None
        if int.from_bytes(frame[0:2], "big") != 0x0800:
            return None
        return frame[20:]
    if linktype in (0, 108):  # BSD loopback / OpenBSD loopback
        if len(frame) < 4:
            return None
        return frame[4:]
    if linktype == 101:  # Raw IP
        return frame
    return None


def _tcp_payload(packet: bytes) -> tuple[bytes, int, int] | None:
    """Parse IPv4/TCP. Return (payload, source port, destination port)."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 15) * 4
    if ihl < 20 or len(packet) < ihl:
        return None
    if packet[9] != 6:
        return None
    total = int.from_bytes(packet[2:4], "big")
    packet = packet[:total] if total else packet
    tcp = packet[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack_from("!HH", tcp, 0)
    offset = (tcp[12] >> 4) * 4
    if offset < 20 or len(tcp) < offset:
        return None
    return tcp[offset:], sport, dport


def parse_pcap(path: Path) -> dict[tuple[str, int, str, int], bytes]:
    data = path.read_bytes()
    if len(data) < 24:
        raise CaptureError("file is shorter than a pcap header")
    magic = data[:4]
    if magic not in PCAP_MAGICS:
        if magic == b"\x0a\x0d\x0d\x0a":
            raise CaptureError(
                "this is a pcapng file; convert it with "
                "'editcap -F pcap input.pcapng output.pcap'"
            )
        raise CaptureError("not a classic pcap file")
    endian = PCAP_MAGICS[magic]
    network = struct.unpack_from(endian + "I", data, 20)[0]
    position = 24
    segments: dict[tuple[str, int, str, int], dict[int, bytes]] = {}
    while position + 16 <= len(data):
        _, _, incl_len, _ = struct.unpack_from(endian + "IIII", data, position)
        position += 16
        frame = data[position:position + incl_len]
        position += incl_len
        if len(frame) < incl_len:
            break
        packet = _payload_from_frame(network, frame)
        if packet is None:
            continue
        parsed = _tcp_payload(packet)
        if parsed is None:
            continue
        payload, sport, dport = parsed
        if PORT not in (sport, dport) or not payload:
            continue
        total = int.from_bytes(packet[2:4], "big")
        ip = packet[:total] if total else packet
        src_ip = socket.inet_ntoa(ip[12:16])
        dst_ip = socket.inet_ntoa(ip[16:20])
        key = (src_ip, sport, dst_ip, dport)
        # A TCP stream can be split or retransmitted; remember the payload by
        # sequence number and reassemble in order below.
        tcp = ip[(ip[0] & 15) * 4:]
        seq = struct.unpack_from("!I", tcp, 4)[0]
        segments.setdefault(key, {})[seq] = payload
    result: dict[tuple[str, int, str, int], bytes] = {}
    for key, chunks in segments.items():
        expected = None
        out = bytearray()
        ok = True
        for seq in sorted(chunks):
            chunk = chunks[seq]
            if expected is None:
                expected = seq
            if seq > expected:
                ok = False
                break
            skip = expected - seq
            out.extend(chunk[skip:])
            expected = max(expected, seq + len(chunk))
        if ok:
            result[key] = bytes(out)
    return result


def iter_frames(data: bytes):
    position = 0
    while position + 2 <= len(data):
        first = data[position]
        position += 1
        command = first >> 4
        if first & 15:
            return
        length = 0
        for index in range(MAX_LENGTH_BYTES):
            if position >= len(data):
                return
            digit = data[position]
            position += 1
            length |= (digit & 127) << (7 * index)
            if length > MAX_FRAME:
                raise CaptureError("oversized long-link frame")
            if not digit & 128:
                break
        else:
            raise CaptureError("malformed long-link length")
        if position + length > len(data):
            return
        yield command, data[position:position + length]
        position += length


def extract(data: bytes) -> list[dict]:
    candidates: dict[tuple[str, str], dict] = {}
    serial_hint: str | None = None
    for command, body in iter_frames(data):
        serial = None
        device = None
        if command == 1 and len(body) >= 5:
            # AUTH-I: version[3], mode, serial length, serial, nonce, sig.
            serial_length = body[4]
            if 0 < serial_length <= 127 and len(body) >= 5 + serial_length:
                serial = body[5:5 + serial_length].decode("ascii", "replace")
        elif command == 7 and len(body) >= 5:
            # Refresh-I: version[3], serial length, serial, 0x20, device id.
            serial_length = body[3]
            index = 4 + serial_length
            if (
                0 < serial_length <= 127
                and len(body) >= index + 33
                and body[index] == 32
            ):
                serial = body[4:index].decode("ascii", "replace")
                device = body[index + 1:index + 33]
        elif command == 3 and len(body) >= 37:
            # AUTH-III existing: version[3], nonce, 0x20, device id, sig.
            if body[4] == 32:
                device = body[5:37]
        if serial:
            serial_hint = serial
        if device:
            key = (serial or serial_hint or "?", device.hex())
            candidates[key] = {
                "serial": serial or serial_hint,
                "device_id_hex": device.hex(),
                "device_id_ascii": (
                    device.decode("ascii") if all(32 <= b < 127 for b in device) else None
                ),
                "source_command": command,
                "length": len(device),
            }
    return list(candidates.values())


def _summary(entry: dict, redact: bool) -> dict:
    if not redact:
        return entry
    serial = entry.get("serial")
    return {
        "serial_sha256_prefix": (
            hashlib.sha256(serial.encode()).hexdigest()[:16] if serial else None
        ),
        "length": entry["length"],
        "sha256_prefix": hashlib.sha256(
            bytes.fromhex(entry["device_id_hex"])
        ).hexdigest()[:16],
        "source_command": entry["source_command"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("capture", help="pcap file, or raw stream with --raw")
    parser.add_argument("--raw", action="store_true",
                        help="treat the input as a raw long-link stream")
    parser.add_argument("--write", help="write the JSON result here (mode 0600)")
    parser.add_argument("--redact", action="store_true",
                        help="print a hash instead of the identity")
    args = parser.parse_args()

    path = Path(args.capture)
    try:
        if args.raw:
            streams = {("raw", 0, "raw", 0): path.read_bytes()}
        else:
            streams = parse_pcap(path)
        found: list[dict] = []
        for (src_ip, src_port, dst_ip, dst_port), data in streams.items():
            for entry in extract(data):
                entry = dict(entry)
                entry["stream"] = f"{src_ip}:{src_port} -> {dst_ip}:{dst_port}"
                found.append(entry)
        # Deduplicate across streams.
        unique = {}
        for entry in found:
            unique[(entry.get("serial"), entry["device_id_hex"])] = entry
        found = list(unique.values())
    except (CaptureError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    full = {
        "capture": str(path),
        "candidates": found,
    }
    display = {
        "capture": str(path),
        "candidates": [_summary(entry, args.redact) for entry in found],
    }
    if args.write:
        target = Path(args.write)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(full, indent=2) + "\n")
        target.chmod(0o600)
        print(f"wrote {target} (mode 0600)", file=sys.stderr)
    print(json.dumps(display, indent=2) + "\n", end="")
    if not found:
        print(
            "no device identity found. The capture must include the legacy "
            "channel-99 handshake (port 8666), not only the newer ECDH "
            "bootstrap. Try starting the capture before re-docking or "
            "power-cycling the device.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
