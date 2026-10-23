#!/usr/bin/env python3
"""Download the firmware package the EZVIZ cloud offers for a device.

The proven CP4 route uses the device's own legacy channel-99 identity:

    1. TCP 8666: AUTH-I/AUTH-II/AUTH-III/AUTH-IV using the serial and the
       device verification code (shown on the device), then DAS redirection.
    2. MQTT 3.1.1 to the cloud-assigned DAS broker, as the device.
    3. Command 10339 with an advertised older firmware version.
    4. Download the returned FileInfo path and check its MD5/size.

Existing-device authentication rotates the device's master/session keys. The
physical device may briefly reconnect or reauthenticate, and its cloud session
can be disrupted. Use this only on a device you own. Do not run it while you
need the camera's cloud connection.

The device identity (32 bytes, usually 32 printable base64 characters) is sent
in the clear during the device's own bootstrap exchange. Extract it from a
capture with device_id.py, or let this tool register a new identity with
--register (which is more disruptive; see --help).

Examples:
    # First run: use a captured identity and save session keys for reuse.
    python tools/get_firmware.py --serial ABC12345 \\
        --model CS-CP4-R100-6E2WPFBS --device-id-file device-id.json \\
        --state session.json --output firmware.dav

    # Later runs refresh the saved identity instead of re-authenticating.
    python tools/get_firmware.py --serial ABC12345 --state session.json \\
        --output firmware.dav

    # Let the tool look up model/current version through an account login.
    python tools/get_firmware.py --serial ABC12345 --account you@example.com \\
        --device-id-file device-id.json --state session.json --output firmware.dav
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from pyezvizapi import _longlink as wire
    from pyezvizapi._longlink_transport import LbsConnection
except ImportError:  # pragma: no cover - exercised by users without the package
    print(
        "error: pyezvizapi is required. Install it with:\n"
        "  python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)

DEFAULT_BOOTSTRAP_HOST = "litedev.eu.ezvizlife.com"
DEFAULT_BOOTSTRAP_PORT = 8666
DEFAULT_API_URL = "apiieu.ezvizlife.com"
FIRMWARE_COMMAND = 10339
FIRMWARE_RESPONSE = 10340


class ProtocolError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client for the DAS broker
# ---------------------------------------------------------------------------


def _mqtt_string(value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ProtocolError("MQTT string too long")
    return struct.pack("!H", len(value)) + value


def _mqtt_remaining(length: int) -> bytes:
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        out.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(out)


class Mqtt:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.next_id = 1
        self.pending: list[tuple[str, bytes]] = []

    def _send(self, first: int, body: bytes) -> None:
        self.sock.sendall(bytes([first]) + _mqtt_remaining(len(body)) + body)

    def _read(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            chunk = self.sock.recv(size - len(out))
            if not chunk:
                raise ProtocolError("MQTT peer closed the connection")
            out.extend(chunk)
        return bytes(out)

    def _read_packet(self) -> tuple[int, bytes]:
        first = self._read(1)[0]
        length = 0
        for index in range(4):
            digit = self._read(1)[0]
            length |= (digit & 127) << (7 * index)
            if length > 4 * 1024 * 1024:
                raise ProtocolError("oversized MQTT packet")
            if not digit & 128:
                return first, self._read(length)
        raise ProtocolError("malformed MQTT remaining length")

    def _publish_payload(self, first: int, data: bytes) -> tuple[str, bytes]:
        qos = (first >> 1) & 3
        topic_length = int.from_bytes(data[0:2], "big")
        position = 2
        topic = data[position:position + topic_length].decode("utf-8", "replace")
        position += topic_length
        if qos:
            packet_id = data[position:position + 2]
            position += 2
            self._send(0x40, packet_id)  # PUBACK
        return topic, data[position:]

    def connect(self, client_id: bytes, username: bytes, password: bytes,
                will: tuple[bytes, bytes] | None, keepalive: int = 30) -> None:
        flags = 0x02  # clean session
        flags |= 0x80 | 0x40  # username, password
        payload = _mqtt_string(client_id)
        if will:
            topic, message = will
            flags |= 0x04 | 0x08 | 0x20  # will, will QoS 1, will retain
            payload += _mqtt_string(topic) + _mqtt_string(message)
        payload += _mqtt_string(username) + _mqtt_string(password)
        body = (
            _mqtt_string(b"MQTT")
            + bytes([4, flags])
            + struct.pack("!H", keepalive)
            + payload
        )
        self._send(0x10, body)
        first, data = self._read_packet()
        if first >> 4 != 2:
            raise ProtocolError("expected CONNACK")
        if len(data) != 2:
            raise ProtocolError("malformed CONNACK")
        if data[1] != 0:
            raise ProtocolError(f"broker refused login (CONNACK {data[1]})")

    def subscribe(self, topic: str, qos: int = 1) -> None:
        packet_id = self.next_id
        self.next_id = (self.next_id + 1) & 0xFFFF or 1
        body = struct.pack("!H", packet_id) + _mqtt_string(topic.encode()) + bytes([qos])
        self._send(0x82, body)
        while True:
            first, data = self._read_packet()
            kind = first >> 4
            if kind == 9:
                if len(data) < 3 or data[2] == 0x80:
                    raise ProtocolError("broker rejected subscription")
                return
            if kind == 3:
                # A retained message can legitimately precede SUBACK.
                self.pending.append(self._publish_payload(first, data))

    def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        body = _mqtt_string(topic.encode())
        if qos:
            packet_id = self.next_id
            self.next_id = (self.next_id + 1) & 0xFFFF or 1
            body += struct.pack("!H", packet_id)
        body += payload
        self._send(0x30 | (qos << 1), body)
        # The PUBACK is consumed by wait_publish(), so a response PUBLISH that
        # races the acknowledgement cannot desynchronise this client.

    def wait_publish(self, timeout: float):
        """Yield (topic, payload) for PUBLISH packets until the deadline."""
        while self.pending:
            yield self.pending.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.sock.settimeout(remaining)
            try:
                first, data = self._read_packet()
            except socket.timeout:
                return
            kind = first >> 4
            if kind == 3:  # PUBLISH
                yield self._publish_payload(first, data)
            elif kind == 13:  # PINGRESP
                continue
            elif kind in (4, 9):  # PUBACK / SUBACK
                continue
            elif kind == 14:
                raise ProtocolError("broker sent DISCONNECT")

    def disconnect(self) -> None:
        try:
            self.sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Device identity / protocol
# ---------------------------------------------------------------------------


def parse_device_id(text: str) -> bytes:
    text = text.strip()
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    raw = text.encode("ascii")
    if len(raw) == 32:
        return raw
    raise ProtocolError(
        "device id must be 32 ASCII characters (the observed form) or 64 hex digits"
    )


def device_id_from_file(path: str) -> bytes:
    text = Path(path).read_text()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return parse_device_id(text)
    if isinstance(obj, dict):
        if obj.get("device_id_hex"):
            return bytes.fromhex(obj["device_id_hex"])
        if obj.get("device_id_ascii"):
            return parse_device_id(obj["device_id_ascii"])
        if len(obj.get("candidates") or []) == 1:
            entry = obj["candidates"][0]
            if entry.get("device_id_hex"):
                return bytes.fromhex(entry["device_id_hex"])
    raise ProtocolError(f"{path} does not contain a single device identity")


def _response(conn: LbsConnection, frame: bytes, expected: int) -> bytes:
    command, payload = conn.exchange(frame)
    if command != expected:
        raise ProtocolError(f"expected long-link command {expected}, got {command}")
    return payload


def authenticate(conn: LbsConnection, serial: bytes, code: bytes,
                 device_id: bytes | None, register: bool) -> tuple[bytes, bytes, bytes, dict]:
    """Run the legacy device handshake and DAS redirect.

    Returns (device_id, master_key, session_key, das_info).
    """
    shared = wire.share_key(code, serial, True)
    n1, n3 = secrets.token_bytes(2)
    # The tested device accepts protocol 1.2.0 with auth mode 0 (serial plus
    # verification code). wire.authentication_i() emits protocol 1.3.0, so the
    # proven frame is built explicitly here.
    auth_i = (
        bytes([1, 2, 0, 0, len(serial)])
        + serial
        + bytes([n1])
        + wire.signature(serial + bytes([n1]), shared)
    )
    n2 = wire.authentication_ii(
        _response(conn, wire.header(1, len(auth_i)) + auth_i, 2),
        serial,
        shared,
        n1,
    )
    if register:
        payload = _response(
            conn, wire.authentication_iii_create(serial, shared, n2, n3), 6
        )
        device_id, master, session = wire.authentication_iv_create(
            payload, serial, shared, bytes([n1, n2, n3])
        )
    else:
        if not device_id or len(device_id) != 32:
            raise ProtocolError("existing-device authentication needs a 32-byte device id")
        payload = _response(
            conn, wire.authentication_iii_existing(serial, shared, device_id, n2, n3), 5
        )
        master, session = wire.authentication_iv_existing(
            payload, serial, shared, bytes([n1, n2, n3])
        )
    payload = _response(conn, wire.das_request(serial, session, mode=0), 11)
    return device_id, master, session, wire.das_response(payload, session)


def refresh(conn: LbsConnection, serial: bytes, device_id: bytes,
            master: bytes) -> tuple[bytes, dict]:
    n1 = secrets.randbelow(256)
    payload = _response(conn, wire.refresh_i(serial, device_id, master, n1), 8)
    n2, session = wire.refresh_ii(payload, master, n1)
    conn.send(wire.refresh_iii(master, n2))
    payload = _response(conn, wire.das_request(serial, session, mode=0), 11)
    return session, wire.das_response(payload, session)


# ---------------------------------------------------------------------------
# Firmware query
# ---------------------------------------------------------------------------


def build_query(serial: str, model: str, firmware_id: str, current_version: str,
                channel: str = "0", ota_type: str = "0") -> bytes:
    root = ET.Element("Request")
    ET.SubElement(root, "DevSerial").text = serial
    ET.SubElement(root, "Model").text = model
    ET.SubElement(root, "Authorization").text = ""
    ET.SubElement(
        root,
        "VersionInfo",
        CurFw=current_version,
        ReqFw="0",
        FirmwareIdentificationCode=firmware_id,
        Serial=serial,
    )
    ET.SubElement(root, "Channel").text = channel
    ET.SubElement(root, "OtaType").text = ota_type
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root)


def envelope(session_key: bytes, sequence: int, body: bytes) -> bytes:
    meta = json.dumps(
        {"CmdVer": "v1.0.0", "Seq": sequence, "MsgType": 0}, separators=(",", ":")
    ).encode()
    return wire.encrypt(session_key, struct.pack("!H", len(meta)) + meta + body)


def parse_response(payload: bytes, session_key: bytes, serial: str) -> dict:
    plain = wire.decrypt(session_key, payload)
    if len(plain) < 4:
        raise ProtocolError("truncated response")
    meta_length = int.from_bytes(plain[:2], "big")
    if meta_length > len(plain) - 2:
        raise ProtocolError("invalid response metadata length")
    raw_meta = plain[2:2 + meta_length].rstrip(b"\x00")
    try:
        metadata = json.loads(raw_meta)
    except (ValueError, UnicodeDecodeError):
        metadata = {}
    body = plain[2 + meta_length:].lstrip(b"\x00\r\n\t ").rstrip(b"\x00")
    root = ET.fromstring(body)
    result = root.findtext("Result")
    parsed = {
        "result": int(result) if result not in (None, "") else None,
        "seq": metadata.get("Seq") if isinstance(metadata, dict) else None,
        "description": root.findtext("Describe") or None,
        "file": None,
        "servers": {},
    }
    file_info = root.find("FileInfo")
    if file_info is not None:
        parsed["file"] = {
            "name": file_info.get("Name"),
            "checksum": file_info.get("CheckSum"),
            "version": file_info.get("Version"),
            "size": int(file_info.get("Size") or 0),
            "description": file_info.get("Describe"),
        }
    for tag, scheme in (("UpgradeSvrHttps", "https"), ("UpgradeSvrHttp", "http")):
        server = root.find(tag)
        if server is not None:
            parsed["servers"][scheme] = {
                "address": server.get("Address"),
                "port": int(server.get("Port") or (443 if scheme == "https" else 80)),
            }
    return parsed


def query_url(parsed: dict) -> str | None:
    file_info = parsed.get("file") or {}
    name = file_info.get("name")
    if not name:
        return None
    for scheme in ("https", "http"):
        server = parsed["servers"].get(scheme)
        if not server or not server.get("address"):
            continue
        port = server.get("port")
        default = 443 if scheme == "https" else 80
        host = server["address"]
        if port and port != default:
            host = f"{host}:{port}"
        return f"{scheme}://{host}{name if name.startswith('/') else '/' + name}"
    return None


def download(url: str, output: Path, expected_md5: str | None,
             expected_size: int | None, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "ezviz-cp4-tools/1.0"})
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=timeout) as response, output.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    result = {
        "url": url,
        "file": str(output),
        "size": size,
        "md5": md5.hexdigest(),
        "sha256": sha256.hexdigest(),
        "md5_expected": expected_md5,
        "size_expected": expected_size,
    }
    problems = []
    if expected_md5 and result["md5"].lower() != expected_md5.lower():
        problems.append("MD5 mismatch")
    if expected_size and size != expected_size:
        problems.append("size mismatch")
    result["verified"] = not problems
    result["problems"] = problems
    if problems:
        # A bad download is not useful; keep the evidence but make it obvious.
        result["kept"] = True
    return result


def will_payload(session_key: bytes, serial: str, model: str, version: str,
                 device_id: bytes, lbs_domain: str) -> bytes:
    identity = device_id.decode("ascii") if all(32 <= b < 127 for b in device_id) else ""
    info = {
        "DevSerial": "",
        "SubSerial": serial,
        "FirmwareVersion": version or "",
        "DevType": model,
        "DevTypeDisplay": model,
        "MAC": "",
        "Status": 0,
        "FirmwareIdentificationCode": "",
        "dev_oeminfo": 0,
        "LbsDomain": lbs_domain,
        "RegMode": 0,
        "SDKMainVersion": "V2.6.0",
        "SDKVersion": {"1000": "V2.6.0", "0": ""},
        "DeviceID": identity,
    }
    return wire.encrypt(
        session_key, json.dumps(info, separators=(",", ":")).encode()
    )


def read_password(env_name: str, prompt: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    raise SystemExit(
        f"no terminal for {prompt.strip()}; set {env_name} or run interactively"
    )


def account_lookup(account: str, password: str, serial: str, api_url: str) -> dict:
    try:
        from pyezvizapi.client import EzvizClient
    except ImportError as error:
        raise SystemExit("pyezvizapi is required for --account") from error
    client = EzvizClient(account=account, password=password, url=api_url, timeout=20)
    client.login()
    info = client.get_device_infos(serial)
    device = info.get("deviceInfos") or {}
    return {
        "model": device.get("deviceType"),
        "version": device.get("version"),
        "name": device.get("name"),
    }


def load_state(path: str | None, serial: str) -> dict:
    if not path:
        return {}
    state_path = Path(path)
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict) or state.get("serial") != serial:
        print(
            f"warning: {path} belongs to a different serial; ignoring it",
            file=sys.stderr,
        )
        return {}
    return state


def state_has_device_id(path: str | None) -> bool:
    if not path:
        return False
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and bool(state.get("device_id_hex"))


def save_state(path: str | None, state: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    target.chmod(0o600)


def variants_from_args(args: argparse.Namespace, account_info: dict) -> list[dict]:
    if args.advertise_version:
        versions = [args.advertise_version]
    else:
        versions = []
        current = args.current_version or account_info.get("version")
        if current:
            versions.append(current)
        # The cloud returns "no update" when the advertised version is current.
        # This deliberately old value forces the latest package.
        versions.append(args.fallback_version)
    ids = [args.firmware_id] if args.firmware_id is not None else ["", "0"]
    seen = set()
    variants = []
    for version in versions:
        for firmware_id in ids:
            key = (version, firmware_id)
            if key in seen:
                continue
            seen.add(key)
            variants.append({"current_version": version, "firmware_id": firmware_id})
    return variants


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--model", help="device model, e.g. CS-CP4-R100-6E2WPFBS")
    parser.add_argument("--device-id", help="32 ASCII characters or 64 hex digits")
    parser.add_argument("--device-id-file",
                        help="JSON from device_id.py, or a file containing the id")
    parser.add_argument("--state", help="JSON session state (mode 0600)")
    parser.add_argument("--register", action="store_true",
                        help="ask the cloud to allocate a new device identity instead "
                             "of using a captured one; more disruptive")
    parser.add_argument("--verification-code-env", default="EZVIZ_DEVICE_CODE",
                        help="environment variable holding the device verification code")
    parser.add_argument("--account", help="EZVIZ account, used only to look up model/version")
    parser.add_argument("--account-password-env", default="EZVIZ_PASSWORD")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--bootstrap-host", default=DEFAULT_BOOTSTRAP_HOST)
    parser.add_argument("--bootstrap-port", type=int, default=DEFAULT_BOOTSTRAP_PORT)
    parser.add_argument("--current-version",
                        help="firmware version to advertise first (default: account value)")
    parser.add_argument("--advertise-version",
                        help="use exactly this version in every query")
    parser.add_argument("--fallback-version", default="V0.0.0 build 000000",
                        help="older version tried when no package is returned")
    parser.add_argument("--firmware-id", default=None,
                        help="FirmwareIdentificationCode; default tries '' then '0'")
    parser.add_argument("--listen", type=float, default=20.0,
                        help="seconds to wait for each query response")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="bootstrap socket timeout")
    parser.add_argument("--download-timeout", type=float, default=60.0)
    parser.add_argument("--output", default=None,
                        help="output .dav path (default: firmware-<serial>.dav)")
    parser.add_argument("--no-download", action="store_true",
                        help="print package metadata without downloading")
    parser.add_argument("--no-will", action="store_true",
                        help="omit the MQTT will (use if the broker rejects login)")
    parser.add_argument("--show-identifiers", action="store_true",
                        help="print the device identity in the report")
    parser.add_argument("--json-out", help="write a report here (mode 0600)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the planned queries without contacting anything")
    args = parser.parse_args()

    if not args.model and not args.account:
        parser.error("--model is required unless --account is used for lookup")
    if args.register and (args.device_id or args.device_id_file):
        parser.error("use either --register or a captured device identity, not both")
    if not args.register and not args.device_id and not args.device_id_file:
        if not state_has_device_id(args.state):
            parser.error(
                "existing-device authentication needs --device-id or "
                "--device-id-file (extract it with tools/device_id.py), or "
                "use --register to allocate a new identity"
            )

    serial = args.serial.strip()
    try:
        serial_bytes = serial.encode("ascii")
    except UnicodeEncodeError:
        parser.error("serial must be ASCII")
    if not 0 < len(serial_bytes) <= 127:
        parser.error("serial must be 1..127 ASCII bytes")

    device_id = None
    if args.device_id_file:
        device_id = device_id_from_file(args.device_id_file)
    elif args.device_id:
        device_id = parse_device_id(args.device_id)

    account_info = {}
    if args.account and not args.dry_run:
        password = read_password(args.account_password_env, "EZVIZ account password: ")
        try:
            account_info = account_lookup(args.account, password, serial, args.api_url)
        except Exception as error:
            print(f"warning: account lookup failed: {error}", file=sys.stderr)
    model = args.model or account_info.get("model")
    current_version = args.current_version or account_info.get("version") or ""

    variants = variants_from_args(args, account_info)
    if args.dry_run:
        print(json.dumps({
            "serial": serial,
            "model": model,
            "device_id_length": len(device_id) if device_id else None,
            "register": args.register,
            "variants": variants,
            "queries": [
                build_query(serial, model or "<model>", item["firmware_id"],
                            item["current_version"]).decode("utf-8", "replace")
                for item in variants
            ],
        }, indent=2))
        return 0

    if not model:
        parser.error("could not determine a model; supply --model")

    state = load_state(args.state, serial)
    state_id = state.get("device_id_hex")
    if state_id:
        try:
            saved_id = bytes.fromhex(state_id)
        except ValueError:
            saved_id = None
        if saved_id is None:
            state = {}
        elif device_id is None:
            device_id = saved_id
        elif saved_id != device_id:
            print(
                "warning: saved state belongs to a different device identity; "
                "ignoring it",
                file=sys.stderr,
            )
            state = {}
    if not args.register and device_id is None:
        print(
            "error: no usable device identity. Extract one with "
            "tools/device_id.py or use --register.",
            file=sys.stderr,
        )
        return 2
    report: dict = {
        "serial": serial,
        "model": model,
        "bootstrap": {"host": args.bootstrap_host, "port": args.bootstrap_port},
        "queries": [],
    }
    if args.show_identifiers and device_id:
        report["device_id_hex"] = device_id.hex()

    # ------------------------------------------------------------------
    # Session establishment
    # ------------------------------------------------------------------
    conn = None
    session_key = None
    master_key = None
    auth_kind = None
    das = None
    try:
        if (not args.register and state.get("master_hex")
                and state.get("session_hex") and device_id):
            try:
                with LbsConnection(
                    socket.create_connection(
                        (args.bootstrap_host, args.bootstrap_port), args.timeout
                    ),
                    timeout=args.timeout,
                ) as conn:
                    session_key, das = refresh(
                        conn, serial_bytes, device_id, bytes.fromhex(state["master_hex"])
                    )
                master_key = bytes.fromhex(state["master_hex"])
                auth_kind = "refresh"
                print("session refreshed from saved state", flush=True)
            except Exception as error:
                print(f"refresh failed ({error}); falling back to full auth",
                      file=sys.stderr)
                conn = None
                session_key = None

        if session_key is None:
            code = read_password(
                args.verification_code_env, "Device verification code: "
            ).strip().encode("ascii")
            with LbsConnection(
                socket.create_connection(
                    (args.bootstrap_host, args.bootstrap_port), args.timeout
                ),
                timeout=args.timeout,
            ) as conn:
                device_id, master_key, session_key, das = authenticate(
                    conn, serial_bytes, code, device_id, args.register
                )
            del code
            auth_kind = "register" if args.register else "existing"
            print(f"authenticated ({auth_kind}); device identity loaded", flush=True)
    except wire.AuthenticationRejected as error:
        print(
            f"error: authentication rejected with status {error.status}. "
            "Check the serial, verification code, device identity, and region.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ProtocolError, ValueError) as error:
        print(f"error: authentication failed: {error}", file=sys.stderr)
        return 1

    report["auth"] = auth_kind
    report["das"] = {"address": das.get("Address"), "port": das.get("Port")}
    save_state(args.state, {
        "serial": serial,
        "device_id_hex": device_id.hex(),
        "master_hex": master_key.hex(),
        "session_hex": session_key.hex(),
        "bootstrap_host": args.bootstrap_host,
        "bootstrap_port": args.bootstrap_port,
        "das": das,
    })

    # ------------------------------------------------------------------
    # MQTT firmware query
    # ------------------------------------------------------------------
    broker = (das["Address"], int(das["Port"]))
    will = None
    if not args.no_will:
        topic = ("/Basic/pu2cenplt/" + serial + "/devinfo").encode().ljust(128, b"\0")
        will = (topic, will_payload(session_key, serial, model, current_version,
                                    device_id, args.bootstrap_host))
    mqtt = None
    try:
        sock = socket.create_connection(broker, args.timeout)
        sock.settimeout(args.timeout)
        mqtt = Mqtt(sock)
        mqtt.connect(
            client_id=device_id,
            username=serial_bytes,
            password=b"test",
            will=will,
        )
        mqtt.subscribe("/" + serial + "/#", qos=1)
        print(f"connected to {broker[0]}:{broker[1]} as the device", flush=True)
        sequence = 32001
        package = None
        for index, variant in enumerate(variants):
            body = build_query(
                serial, model, variant["firmware_id"], variant["current_version"]
            )
            report["queries"].append(dict(variant))
            mqtt.publish(
                "/1100/" + str(FIRMWARE_COMMAND),
                envelope(session_key, sequence, body),
                qos=1,
            )
            print(
                f"query {index + 1}/{len(variants)}: advertised "
                f"{variant['current_version']!r}, firmware id "
                f"{variant['firmware_id']!r}",
                flush=True,
            )
            deadline = time.monotonic() + args.listen
            while time.monotonic() < deadline:
                for topic, payload in mqtt.wait_publish(
                    max(0.1, deadline - time.monotonic())
                ):
                    if not topic.endswith("/" + str(FIRMWARE_RESPONSE)):
                        continue
                    try:
                        parsed = parse_response(payload, session_key, serial)
                    except (ValueError, ET.ParseError, ProtocolError) as error:
                        print(f"  ignoring undecodable response: {error}", file=sys.stderr)
                        continue
                    if parsed.get("seq") not in (None, sequence):
                        print(
                            f"  ignoring response for sequence {parsed['seq']}",
                            file=sys.stderr,
                        )
                        continue
                    report["queries"][-1]["result"] = parsed["result"]
                    if parsed["result"] == 0 and parsed["file"] and parsed["file"].get("name"):
                        package = parsed
                        print(
                            f"  package {parsed['file']['version']} "
                            f"({parsed['file']['size']} bytes, "
                            f"MD5 {parsed['file']['checksum']})",
                            flush=True,
                        )
                        break
                    print(f"  result {parsed['result']} (no package)", flush=True)
                if package:
                    break
                # Re-enter the wait loop if there are messages but no package.
                if time.monotonic() >= deadline:
                    break
            sequence += 1
            if package:
                break
    except (OSError, ProtocolError) as error:
        print(f"error: MQTT query failed: {error}", file=sys.stderr)
        return 1
    finally:
        if mqtt:
            mqtt.disconnect()

    if not package:
        print(
            "no firmware package was returned. The cloud may have no update "
            "for the advertised version; try --advertise-version with an "
            "older build string.",
            file=sys.stderr,
        )
        report["package"] = None
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")
            Path(args.json_out).chmod(0o600)
        return 1

    file_info = package["file"]
    url = query_url(package)
    report["package"] = dict(file_info)
    report["package"]["url"] = url
    if args.no_download or not url:
        print(json.dumps(report, indent=2))
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")
            Path(args.json_out).chmod(0o600)
        return 0

    output = Path(args.output or f"firmware-{serial}.dav")
    try:
        result = download(
            url,
            output,
            file_info.get("checksum"),
            file_info.get("size") or None,
            args.download_timeout,
        )
    except (OSError, urllib.error.URLError) as error:
        print(f"error: download failed: {error}", file=sys.stderr)
        return 1
    report["download"] = result
    print(json.dumps(report, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")
        Path(args.json_out).chmod(0o600)
    if not result["verified"]:
        print("DOWNLOAD VERIFICATION FAILED", file=sys.stderr)
        return 1
    print(f"saved {output} ({result['size']} bytes, MD5 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
