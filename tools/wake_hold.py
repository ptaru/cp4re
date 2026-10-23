"""Camera-side RAM sleep holds for the supported CP4 ezapp."""
from __future__ import annotations

import secrets
import subprocess

# CP4 V5.3.3 build 251118. Never use this address with another executable.
EZAPP_MD5 = "8a4f3c1bf4f850318a17fae01305c20b"
SLEEP_OFFSET = 6753236


def hold_script(seconds: int | str | None) -> str:
    """None releases; 'forever' acquires without a timer. No shell input interpolation."""
    if seconds not in (None, "forever") and (type(seconds) is not int or not 1 <= seconds <= 300):
        raise ValueError("hold must be 1..300 seconds or forever")
    action = "release" if seconds is None else "acquire"
    duration = 0 if seconds in (None, "forever") else seconds
    token = secrets.token_hex(16)
    return r'''
set -eu
state=/tmp/cp4-wake-hold
lock=/tmp/cp4-wake-hold.lock
fail() { echo "HOLD_ERROR=$1"; exit 1; }
locked=0
cleanup() { if [ "$locked" = 1 ]; then rmdir "$lock"; fi; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mkdir "$lock" 2>/dev/null || fail busy
locked=1
pid=$(ps | awk '$NF=="/dav/ezapp" {print $1}')
case "$pid" in ''|*[!0-9]*) fail no_unique_ezapp;; esac
[ "$(md5sum /dav/ezapp | awk '{print $1}')" = 'EXPECTED_MD5' ] || fail unsupported_ezapp
start=$(awk '{print $22}' /proc/$pid/stat)
[ -n "$start" ] || fail process_missing
read_flag() { dd if=/proc/$pid/mem bs=4 skip=WORD_OFFSET count=1 2>/dev/null | hexdump -v -e '1/4 "%u"'; }
write_flag() { printf "$1" | dd of=/proc/$pid/mem bs=1 seek=OFFSET count=4 conv=notrunc 2>/dev/null; }
same_process() { [ "$(awk '{print $22}' /proc/$pid/stat 2>/dev/null)" = "$start" ]; }
flag=$(read_flag)
if [ 'ACTION' = release ]; then
    if [ ! -d "$state" ]; then
        echo HOLD_RESULT=no_owned_hold
        exit 0
    fi
    [ "$(cat "$state/pid")" = "$pid" ] && [ "$(cat "$state/start")" = "$start" ] || fail stale_owner
    case "$flag" in 0|1) ;; *) fail unexpected_sleep_flag;; esac
    same_process || fail process_changed
    if [ "$flag" = 0 ]; then write_flag '\001\000\000\000'; fi
    [ "$(read_flag)" = 1 ] || fail restore_failed
    rm -r "$state"
    echo HOLD_RESULT=released
    exit 0
fi
[ ! -e "$state" ] || fail already_held
[ "$flag" = 1 ] || fail sleep_inhibited_by_another_owner
mkdir "$state"
printf '%s\n' "$pid" > "$state/pid"
printf '%s\n' "$start" > "$state/start"
printf '%s\n' 'TOKEN' > "$state/token"
printf '%s\n' 'DURATION' > "$state/seconds"
# ezapp may re-enable sleep after wake. Maintain the flag, not just its initial value.
(
    trap - EXIT
    trap '' HUP
    stopping=0
    trap 'stopping=1' TERM INT
    locked=0
    read clock rest </proc/uptime
    deadline=$(( ${clock%%.*} + DURATION ))
    while :; do
        while ! mkdir "$lock" 2>/dev/null; do sleep 1; done
        locked=1
        trap cleanup EXIT
        [ "$(cat "$state/token" 2>/dev/null)" = 'TOKEN' ] || exit 0
        same_process || exit 0
        [ "$(cat "$state/pid")" = "$pid" ] && [ "$(cat "$state/start")" = "$start" ] || exit 0
        current=$(read_flag)
        case "$current" in 0|1) ;; *) echo HOLD_ERROR=unexpected_sleep_flag; exit 1;; esac
        read clock rest </proc/uptime
        if [ "$stopping" = 1 ] || { [ DURATION -gt 0 ] && [ "${clock%%.*}" -ge "$deadline" ]; }; then
            write_flag '\001\000\000\000'
            [ "$(read_flag)" = 1 ] || exit 1
            rm -r "$state"
            echo HOLD_RESULT=expired
            exit 0
        fi
        write_flag '\000\000\000\000'
        [ "$(read_flag)" = 0 ] || exit 1
        cleanup
        locked=0
        sleep 1
    done
) </dev/null >/tmp/cp4-wake-hold-worker.log 2>&1 &
printf '%s\n' "$!" > "$state/worker"

same_process || fail process_changed
write_flag '\000\000\000\000'
[ "$(read_flag)" = 0 ] || fail inhibit_failed
echo HOLD_RESULT=active
echo HOLD_SECONDS=DURATION
'''.replace("EXPECTED_MD5", EZAPP_MD5).replace("WORD_OFFSET", str(SLEEP_OFFSET // 4)).replace("OFFSET", str(SLEEP_OFFSET)).replace(
        "ACTION", action).replace("TOKEN", token).replace("DURATION", str(duration))


def run_hold(host: str, seconds: int | str | None, port: int = 2222,
             identity: str | None = None) -> dict:
    command = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o",
               "ConnectTimeout=5", "-o", "ServerAliveInterval=2", "-o",
               "ServerAliveCountMax=2"]
    if identity:
        command += ["-i", identity]
    command += ["--", f"root@{host}", "sh", "-s"]
    result = {"action": "release" if seconds is None else "hold", "ok": False}
    if seconds is not None:
        result["duration"] = seconds
    try:
        proc = subprocess.run(command, input=hold_script(seconds), text=True,
                              capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = f"SSH result unavailable: {error}. The remote operation may have run; it was not retried."
        return result
    fields = dict(line.split("=", 1) for line in proc.stdout.splitlines()
                  if line.startswith("HOLD_") and "=" in line)
    result["status"] = fields.get("HOLD_RESULT")
    result["ok"] = proc.returncode == 0 and result["status"] in ("active", "released", "no_owned_hold")
    if not result["ok"]:
        result["error"] = fields.get("HOLD_ERROR") or (proc.stderr.strip() or "No confirmed SSH result; remote operation may have run. It was not retried.")
    return result
