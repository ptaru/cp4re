#!/usr/bin/env python3
"""Wake, upload to SD, and launch the CP4 video PoC (no boot-hook changes)."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

# Reuse the public wake tool's packet profiles without cloud dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wake import LAN_PROTOCOLS

# No code patches, SDK calls, persistent config, or boot-hook modifications.
# Stage the binary so an existing running executable is never overwritten.
REMOTE_SCRIPT = r'''
set -eu
[ "$(md5sum /dav/ezapp | awk '{print $1}')" = 8a4f3c1bf4f850318a17fae01305c20b ] || exit 20
awk '$2=="/mnt/mmc01" {found=1} END {exit !found}' /proc/mounts || exit 25
if [ -f /tmp/cp4-video.pid ]; then
    old=$(cat /tmp/cp4-video.pid)
    case "$old" in ''|*[!0-9]*) exit 21;; esac
    if [ -r /proc/$old/cmdline ] && [ "$(tr '\000' '\n' </proc/$old/cmdline | sed -n '1p')" = /mnt/mmc01/cp4-video ]; then
        kill "$old"
        sleep 1
    fi
fi
pid=$(ps | awk '$NF=="/dav/ezapp" {print $1}')
[ -n "$pid" ] || exit 22
handoff=0
restore() {
    if [ "$handoff" = 0 ]; then
        printf '\001\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null
    fi
}
trap restore EXIT
trap 'exit 1' HUP INT TERM
printf '\000\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null
cat > /mnt/mmc01/cp4-video.new
chmod 755 /mnt/mmc01/cp4-video.new
mv /mnt/mmc01/cp4-video.new /mnt/mmc01/cp4-video
# Ignore SSH hangup only in the server's launching subshell.
(trap '' HUP; exec /mnt/mmc01/cp4-video PORT </dev/null >/tmp/cp4-video.log 2>&1) &
server=$!
echo "$server" > /tmp/cp4-video.pid
for attempt in 1 2 3 4 5; do
    if grep -q 'cp4-video listening' /tmp/cp4-video.log; then
        handoff=1
        cat /tmp/cp4-video.log
        exit 0
    fi
    kill -0 "$server" || exit 23
    sleep 1
done
kill "$server" || true
exit 24
'''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True, help='camera IPv4 address or hostname')
    p.add_argument('--protocol', choices=LAN_PROTOCOLS, default='oem',
                   help='oem: UDP 50161 (default); cp4: patched receiver on UDP 50162')
    p.add_argument('--dry-run', action='store_true', help='print the plan without waking or deploying')
    p.add_argument('--identity')
    p.add_argument('--port', type=int, default=8080)
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error('port must be 1..65535')
    wake_port, payload_hex = LAN_PROTOCOLS[args.protocol]
    plan = {'host': args.host, 'protocol': args.protocol, 'wake_port': wake_port,
            'payload_hex': payload_hex, 'stream_url': f'http://{args.host}:{args.port}/live.ps',
            'binary': str(Path(__file__).with_name('cp4-video-arm'))}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    try:
        binary = Path(plan['binary']).read_bytes()
    except FileNotFoundError:
        p.error('ARM binary is missing; run sh tools/video_server/build.sh first')
    ssh = ['ssh', '-p', '2222', '-o', 'BatchMode=yes', '-o',
           'ConnectTimeout=2', '-o', 'ServerAliveInterval=3', '-o',
           'ServerAliveCountMax=2', '-o', 'StrictHostKeyChecking=accept-new']
    if args.identity:
        ssh += ['-i', args.identity]
    ssh += [f'root@{args.host}']
    script = REMOTE_SCRIPT.replace('PORT', str(args.port))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wake:
        print(f'Waking {args.host} using {args.protocol} (UDP {wake_port}); '
              'waiting for SSH and SD card (up to 60 seconds)…',
              flush=True)
        deadline = time.monotonic() + 60
        attempt = 0
        wake_packets = 0
        last_wake = 0.0
        wake_interval = 5.0
        camera_seen = False
        unreachable_since = 0.0
        last_error = ''
        while time.monotonic() < deadline:
            attempt += 1
            now = time.monotonic()
            # The camera counts every matched wake packet, including packets sent
            # while Linux is already awake. Send only while it has not answered,
            # or after it has been unreachable long enough to have slept again.
            should_wake = wake_packets == 0
            if now - last_wake >= wake_interval:
                if not camera_seen:
                    should_wake = True
                elif unreachable_since and now - unreachable_since >= 8.0:
                    should_wake = True
            if should_wake:
                wake.sendto(bytes.fromhex(payload_hex), (args.host, wake_port))
                wake_packets += 1
                last_wake = now
            print(f'  Attempt {attempt} (wake packets {wake_packets}): '
                  'connecting and deploying over SSH…', flush=True)
            try:
                result = subprocess.run(ssh + [script], input=binary,
                                        capture_output=True, timeout=25)
            except subprocess.TimeoutExpired:
                raise SystemExit('SSH deployment did not finish within 25 seconds. '
                                 'The remote exit trap attempts to restore the temporary hold.')
            if result.returncode == 255:
                # SSH transport failed; the camera may be asleep. Start the
                # unreachable timer used to decide whether another wake is needed.
                if not unreachable_since:
                    unreachable_since = time.monotonic()
            else:
                # SSH answered: Linux is up. Do not spend more wake packets on
                # ezapp/SD readiness retries.
                camera_seen = True
                unreachable_since = 0.0
            if result.returncode == 0:
                print(result.stdout.decode(), end='', flush=True)
                print(f'Stream: http://{args.host}:{args.port}/live.ps', flush=True)
                print('Connect within 10 seconds, or wake/relaunch. No boot hook was changed.', flush=True)
                break
            last_error = result.stderr.decode(errors='replace').strip()
            if result.returncode == 255 and any(message in last_error.lower() for message in (
                    'permission denied', 'host key verification failed',
                    'remote host identification has changed', 'no such identity')):
                raise SystemExit(f'SSH authentication/configuration failed:\n{last_error}\n'
                                 'Load your SSH key with ssh-add, or pass --identity /path/to/key.')
            if result.returncode not in (22, 25, 255):
                raise SystemExit(f'Deployment failed ({result.returncode}): '
                                 + (last_error or result.stdout.decode(errors='replace')))
            reason = {22: 'ezapp is not ready yet', 25: 'SD card is not mounted yet'}.get(
                result.returncode, last_error or 'SSH connection failed')
            print(f'  {reason}; retrying.', file=sys.stderr, flush=True)
            time.sleep(.25)
        else:
            raise SystemExit('Camera deployment did not become ready within 60 seconds.\n'
                             + last_error)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
