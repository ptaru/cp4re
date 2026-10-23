#!/usr/bin/env python3
"""Read Hi3861L status using a temporary verified Linux RAM reply patch. No MCU flash.

Requires the patched CP4 receiver and key-only root SSH on TCP 2222.
Default status reads temporarily patch the Linux reply switch, validate HCC
before/after, and restore the original instructions. No MCU flash is performed.
See docs/HI3861-LAN-PATCH.md. Use --no-wake if already awake.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
BINARY = HERE / "cp4-independent-status"
REMOTE_DIR = "/mnt/mmc01/hi3861-flash"
REMOTE_BINARY = REMOTE_DIR + "/independent-cp4-hi3861-readback"
WAKE_PACKET = bytes.fromhex("43503457414b45015003bd2f")
SSH_PORT = 2222

# The public wake_hold tool maintains the sleep flag every second; a one-shot
# write is not sufficient because ezapp can re-enable sleep after a wake.
ROOT = HERE.parents[1]
try:
    sys.path.insert(0, str(ROOT / "tools"))
    from wake_hold import run_hold
except Exception:
    run_hold = None


def acquire_hold(args) -> bool:
    """Return True only if this process owns a hold it should release."""
    if args.use_existing_hold:
        print("using the existing external hold; not acquiring or releasing", flush=True)
        return False
    if run_hold is None:
        raise SystemExit(
            "public tools/wake_hold.py not found; use --use-existing-hold with a hold you manage"
        )
    seconds = args.hold if args.hold == "forever" else int(args.hold)
    result = run_hold(args.host, seconds, SSH_PORT, args.identity)
    if result.get("ok"):
        print(f"external hold active ({seconds})", flush=True)
        return True
    error = str(result.get("error") or result)
    if "already_held" in error:
        print("an existing public hold is active; using it without releasing it", flush=True)
        return False
    raise SystemExit(f"could not acquire external hold: {error}")


def release_hold(args) -> None:
    if run_hold is None:
        return
    result = run_hold(args.host, None, SSH_PORT, args.identity)
    if result.get("ok") and result.get("status") in ("released", "no_owned_hold"):
        print("external hold released", flush=True)
    else:
        print(
            f"WARNING: hold release not confirmed: {result.get('error') or result}",
            file=sys.stderr,
        )


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def ssh_command(args) -> list[str]:
    cmd = [
        "ssh", "-p", str(SSH_PORT),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=3",
        "-o", "ServerAliveInterval=3",
        "-o", "ServerAliveCountMax=2",
    ]
    if args.identity:
        cmd += ["-i", args.identity]
    cmd += [f"root@{args.host}"]
    return cmd


def run_text(args, script: str, timeout: float = 30, check: bool = True) -> str:
    proc = subprocess.run(
        ssh_command(args) + [script],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"ssh exit {proc.returncode}")
    return proc.stdout


def stage_binary(args) -> str:
    if not BINARY.exists():
        raise SystemExit(f"missing {BINARY}; run ./build.sh first")
    data = BINARY.read_bytes()
    digest = hashlib.md5(data).hexdigest()
    try:
        existing = run_text(
            args,
            f"md5sum {REMOTE_BINARY} 2>/dev/null | awk '{{print $1}}'",
            timeout=10, check=False,
        ).strip()
    except (subprocess.TimeoutExpired, OSError):
        existing = ""
    if existing == digest:
        print("remote readback binary already matches; skipping upload", flush=True)
        return digest
    print(f"staging {BINARY.name} ({len(data)} bytes, md5 {digest})", flush=True)
    script = f"""set -eu
awk '$2=="/mnt/mmc01" {{f=1}} END {{exit !f}}' /proc/mounts
mkdir -p {REMOTE_DIR}
if ps | grep -q '[l]auncher -mode'; then echo FLASH_LAUNCHER_RUNNING >&2; exit 2; fi
cat > {REMOTE_BINARY}.new
chmod 500 {REMOTE_BINARY}.new
mv {REMOTE_BINARY}.new {REMOTE_BINARY}
[ "$(md5sum {REMOTE_BINARY} | awk '{{print $1}}')" = '{digest}' ]
"""
    proc = subprocess.run(
        ssh_command(args) + [script],
        input=data, capture_output=True, timeout=90,
    )
    if proc.returncode:
        raise SystemExit(
            "staging failed: "
            + (proc.stderr or proc.stdout).decode(errors="replace").strip()
        )
    print("staged and md5-verified", flush=True)
    return digest


def wait_for_ssh(args, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if "READY" in run_text(args, "echo READY", timeout=6, check=False):
                return
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(0.4)
    raise SystemExit(
        "SSH did not come up after one LAN wake. No further wake packets were sent. "
        "Wake the camera with the authorized cloud helper and rerun with --no-wake."
    )


def wake_once(args) -> None:
    if args.no_wake:
        print("--no-wake: assuming the camera is already awake", flush=True)
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(bytes.fromhex("43503457414b45015003bd2f"), (args.host, 50162))
    print("sent one LAN wake packet", flush=True)
    wait_for_ssh(args, args.wake_wait)


def printable_strings(data: bytes) -> str:
    out = []
    run = bytearray()

    def flush():
        if len(run) >= 4:
            out.append(run.decode("ascii", "replace"))
        run.clear()

    for c in data:
        if c in (10, 13):
            flush()
        elif c == 9 or 32 <= c < 127:
            run.append(c)
        else:
            flush()
    flush()
    return "\n".join(out) + ("\n" if out else "")


def hex_dump(data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {hexpart} |{text}|")
    return "\n".join(lines) + "\n"


def save_outputs(stamp_value: str, label: str, stdout: str, stderr: str) -> dict:
    result = None
    for line in stdout.splitlines():
        if line.startswith("READBACK_RESULT "):
            result = json.loads(line[len("READBACK_RESULT "):])
    local = HERE / f"readback-{stamp_value}-{label}"
    if local.exists():
        raise SystemExit(f"local output directory already exists: {local}")
    local.mkdir()
    (local / "tool.log").write_text(stdout + "\n--- stderr ---\n" + stderr)
    if result is None:
        raise SystemExit(f"no READBACK_RESULT in tool output; see {local / 'tool.log'}")
    manifest = result["manifest"]
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    reason_hex = result.get("wakeup_reason_hex")
    if reason_hex:
        (local / "wakeup_reason.bin").write_bytes(bytes.fromhex(reason_hex))

    outputs = (
        ("status_b64", "status.bin", "hex"),
        ("wakeup_info_b64", "wakeup_info.bin", "hex"),
        ("fw_log_b64", "fw_log.bin", "text"),
        ("awake_packet_b64", "awake_packet.bin", "text"),
    )
    for key, name, style in outputs:
        value = result.get(key)
        if not value:
            continue
        data = base64.b64decode(value)
        (local / name).write_bytes(data)
        stem = name.rsplit(".", 1)[0]
        (local / f"{stem}.hex").write_text(hex_dump(data))
        if style == "text":
            (local / f"{stem}.txt").write_text(printable_strings(data))
        if name == "awake_packet.bin":
            count = data.count(bytes.fromhex("50030000bd0c2f"))
            print(f"awake packet buffer: {count} occurrence(s) of the 7-byte wake header")

    print(f"\nlocal results: {local}")
    print(f"remote results: {manifest.get('out_dir')}")
    print(f"remote log:     {manifest.get('log_path')}")
    requested = set(manifest.get("requested_fields") or [])
    print(f"result:         {manifest.get('result')} (fields: {','.join(sorted(requested))})")
    if "reason" in requested and reason_hex:
        print(f"wakeup reason:  {int(reason_hex, 16)} (0x{reason_hex}); LAN is 4")
    for field, item in (("info", "wakeup_info"), ("fwlog", "fw_log"), ("awake", "awake_packet")):
        if field not in requested:
            continue
        entry = manifest.get(item, {})
        print(f"{item:14s}: return={entry.get('return_hex')} bytes={entry.get('bytes')} sha256={entry.get('sha256')}")
    logcfg = manifest.get("logcfg")
    if logcfg and logcfg.get("requested"):
        print(f"logcfg:         on={logcfg.get('on_return_hex')} off={logcfg.get('off_return_hex')} skipped={logcfg.get('skipped_off')}")
    errors = manifest.get("errors") or []
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return {"local": local, "manifest": manifest, "result": result}


def print_log_hits(local: Path) -> None:
    path = local / "fw_log.txt"
    if not path.exists():
        return
    patterns = ("defense", "last_times", "cur_times", "snd ezlink alarm", "wakeup cpu", "wakeup")
    hits = []
    for line in path.read_text(errors="replace").splitlines():
        if any(p.lower() in line.lower() for p in patterns):
            hits.append(line)
    if hits:
        print("\nfirmware-log hits:")
        for line in hits[:60]:
            print("  " + line)
    else:
        print("\nno known defense/wake strings found in fw_log.txt; inspect the raw log")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="camera LAN address")
    parser.add_argument("--identity", help="optional SSH identity file")
    parser.add_argument("--label", default="manual", help="phase label, e.g. baseline or after99")
    parser.add_argument("--fields", default="status",
                        help="readback fields: status or adapter-probe (also reason,info,fwlog,awake)")
    parser.add_argument("--enable-log", action="store_true",
                        help="set MCU logcfg to 1 before fetching fw_log, then 0")
    parser.add_argument("--hold", default="300",
                        help="public wake_hold duration: 1..300 seconds or forever")
    parser.add_argument("--use-existing-hold", action="store_true",
                        help="do not acquire/release a hold; require one already active")
    parser.add_argument("--adopt-hold", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-wake", action="store_true", help="do not send a LAN wake packet")
    parser.add_argument("--wake-wait", type=float, default=25.0,
                        help="seconds to wait for SSH after one LAN wake")
    parser.add_argument("--check-only", action="store_true",
                        help="resolve and verify addresses; no library calls")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="timeout for the readback SSH command")
    args = parser.parse_args()
    args.use_existing_hold = args.use_existing_hold or args.adopt_hold
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    if not fields or any(f not in ("reason", "info", "fwlog", "awake", "status", "adapter-probe") for f in fields):
        parser.error("--fields must be a comma-separated subset of reason,info,fwlog,awake")
    if "status" in fields and "adapter-probe" in fields: parser.error("select status or adapter-probe, not both")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", args.label):
        parser.error("--label must be 1..40 characters from A-Za-z0-9._-")
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be 1..600 seconds")
    if not 5 <= args.wake_wait <= 120:
        parser.error("--wake-wait must be 5..120 seconds")
    if args.hold != "forever":
        try:
            hold_seconds = int(args.hold)
        except ValueError:
            parser.error("--hold must be 1..300 or forever")
        if not 1 <= hold_seconds <= 300:
            parser.error("--hold must be 1..300 or forever")
        if not args.use_existing_hold and args.timeout + 30 > hold_seconds:
            parser.error("--timeout must be at least 30 seconds less than --hold")

    wake_once(args)

    if args.check_only:
        owned = acquire_hold(args)
        try:
            digest = stage_binary(args)
            binary_check = f"[ \"$(md5sum {REMOTE_BINARY} | awk '{{print $1}}')\" = '{digest}' ]"
            script = f"set -eu; {binary_check}; {REMOTE_BINARY} -mode check -log {REMOTE_DIR}/readback-check.log"
            output = run_text(args, script, timeout=60)
            print(output, end="")
            return 0
        finally:
            if owned:
                release_hold(args)

    # The public wake_hold worker re-asserts the sleep flag every second; use
    # it rather than a one-shot write that ezapp can overwrite.
    owned = acquire_hold(args)
    leave_hold = False
    try:
        digest = stage_binary(args)
        binary_check = f"[ \"$(md5sum {REMOTE_BINARY} | awk '{{print $1}}')\" = '{digest}' ]"

        remote_stamp = stamp()
        remote_out = f"{REMOTE_DIR}/readback-{remote_stamp}"
        remote_log = f"{REMOTE_DIR}/readback-{remote_stamp}.log"
        flags = " -external-hold"
        if args.enable_log:
            flags += " -enable-log"
        script = (
            f"set -eu; {binary_check}; [ ! -e {remote_out} ]; "
            f"{REMOTE_BINARY} -mode readback -label {args.label} "
            f"-out {remote_out} -log {remote_log} -worker-seconds 30 "
            f"-fields {','.join(fields)}{flags}"
        )
        print("running readback (external hold; no updater call)", flush=True)
        try:
            proc = subprocess.run(
                ssh_command(args) + [script],
                capture_output=True, text=True, timeout=args.timeout,
            )
        except subprocess.TimeoutExpired as error:
            leave_hold = True
            print(f"readback SSH command timed out: {error}", file=sys.stderr)
            print(
                "leaving the hold in place so the remote tool can finish and log; "
                "the bounded hold will expire on its own",
                file=sys.stderr,
            )
            if owned and args.hold == "forever":
                print(
                    "WARNING: --hold forever was used; release it manually with "
                    "tools/wake.py --release-hold",
                    file=sys.stderr,
                )
            return 3
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        for line in stdout.splitlines():
            if not line.startswith("READBACK_RESULT "):
                print(line)
        if stderr:
            print(stderr, file=sys.stderr, end="")
        try:
            saved = save_outputs(remote_stamp, args.label, stdout, stderr)
        except SystemExit as error:
            print(error, file=sys.stderr)
            return 2
        print_log_hits(saved["local"])
        if proc.returncode != 0 or saved["manifest"].get("result") != "OK":
            print("readback was partial; see the manifest errors and raw fw_log.bin", file=sys.stderr)
            return 1
        return 0
    finally:
        if owned and not leave_hold:
            release_hold(args)


if __name__ == "__main__":
    raise SystemExit(main())
