# Wake and sleep

## LAN protocols

Choose the packet profile for the camera's MCU firmware:

| Profile | Firmware | UDP port | Exact payload |
| --- | --- | ---: | --- |
| `--protocol cp4` | [Patched CP4 receiver](HI3861-LAN-PATCH.md) | 50162 | `43503457414b45015003bd2f` |
| `--protocol oem` (default) | Unmodified CP4 firmware | 50161 | `50030000bd0c2f` |

The patched receiver starts independently of Linux's pattern setup, accepts the
exact 12-byte packet, requests a 20-second OEM Wi-Fi listening boost and asserts
the Linux wake GPIO. It has a one-second assertion cooldown, no cumulative wake
limit and no application packet store to drain. The network stack still has
finite resources. The payload is not authentication; keep access LAN-restricted.
The listening boost does not hold Linux awake or prove a successful TCP connection.

Send one packet per wake attempt and poll readiness separately. The tool does
not automatically detect installed firmware or send both profiles. Override
`--lan-port` and/or `--payload` only when needed for another supported protocol.

```sh
# Patched receiver, LAN only.
python tools/wake.py --host 192.168.1.50 --protocol cp4 --method lan

# Patched receiver, then cloud preview if LAN verification fails.
python tools/wake.py --host 192.168.1.50 --protocol cp4 \
  --serial ABC12345 --account you@example.com

# Unmodified firmware.
python tools/wake.py --host 192.168.1.50 --protocol oem --method lan

# Inspect the request without sending or connecting.
python tools/wake.py --host 192.168.1.50 --protocol cp4 --dry-run
```

## Cloud wake

The default cloud trigger is the live-view VTM preview handshake. It needs an
owner account and serial; LAN-only use needs neither. `--method auto` tries LAN
first and falls back to cloud when credentials are supplied; `--method cloud`
uses cloud only. `--cloud-mode action|both` also exposes the Wi-Fi listening
request, but that request alone is not a demonstrated Linux wake trigger.
The patched LAN receiver invokes the MCU prewake helper directly, without a
cloud request.

The password comes from `EZVIZ_PASSWORD` or a hidden terminal prompt. Host and
serial have no defaults, and account credentials are not saved. `--json-out`
saves the requested report, including the host/serial supplied by the caller.

## Readiness and timing

`--wait SECONDS` controls verification; zero sends only. `--check-port PORT` is
repeatable and defaults to TCP 2222 and 2323. Use a port that actually exists
on your image: stock firmware has neither SSH nor the custom shell. The custom
TinySSH image uses 2222; a closed TCP port does not prove the SoC failed to wake.
The charging indicator can change while networking remains unreachable.

Linux startup, Wi-Fi/HCC readiness and TCP handshakes are separate events.
Boot traces on the tested image showed the SSH listener launched near 1.3 s and
listening near 2.2 s, while external connection completion occurred later.
Short connection probes can add apparent timing variability. Increase `--wait`
and preserve a failing boot trace rather than repeatedly sending wake packets.
The patched receiver's diagnostic snapshot reports state and helper calls, not
physical wake edges or a TCP-level success guarantee.

## Awake holds

With the custom SSH image installed, add `--hold` to wake the camera and inhibit
sleep. A timed hold accepts 1–300 seconds. `forever` remains active until
explicitly released or Linux restarts:

```sh
python tools/wake.py --host 192.168.1.50 --protocol cp4 --method lan --hold 120
python tools/wake.py --host 192.168.1.50 --protocol cp4 --method lan --hold forever
python tools/wake.py --host 192.168.1.50 --release-hold
```

If the camera is already awake, `--hold` applies without sending another wake.
Otherwise it follows the selected LAN/cloud wake method, waits for SSH, and
applies the hold. The duration starts when sleep is inhibited; the command
returns after confirmation. With `--hold`, readiness uses `--ssh-port` (default
2222) rather than `--check-port`. A positive `--wait` and `--host` are required.

`--release-hold` connects directly over SSH. It sends no LAN or cloud wake and
performs no readiness polling. Releasing when this tool has no hold is a
successful no-op; an unreachable camera produces an SSH error.

SSH uses root, key authentication, and the normal SSH host-key policy. Use
`--identity PATH` for a private key and `--ssh-port PORT` for another port. The
local OpenSSH client is required. Stock firmware has no SSH server.

The helper checks the exact supported CP4 `ezapp` hash before accessing its RAM
sleep flag. It records ownership in `/tmp/cp4-wake-hold`, including the process
ID, process start time and a unique token. Existing holds are refused: release
first to change the duration. A sleep flag already disabled by another tool
is also refused and will not be released by this helper.

A detached camera-side worker reasserts the RAM flag once per second because
`ezapp` can re-enable sleep shortly after wake. Both timed and indefinite holds
use this worker and survive client disconnects. Timed holds restore sleep
permission at their MCU-independent Linux-uptime deadline; indefinite holds run
until released. Ownership checks prevent an old worker from changing a newer
hold. Neither kind survives Linux reboot or power loss. Holds
change RAM only, with no saved policy, boot-hook or firmware changes. Keeping
the camera awake increases battery use. Releasing permits sleep; it does not
force immediate sleep.

The JSON `hold` object reports `ok`, `status`, and an error when applicable.
Wake can succeed while hold setup fails; this returns a nonzero exit status.
An SSH timeout can leave the remote result uncertain and is never automatically
retried. Sleep permission may be restored before a release confirmation reaches
the client. After timed expiry, SSH becoming unreachable is consistent with
sleep but does not independently confirm restoration. `--dry-run` makes no
connections and describes the requested hold or release.


## Troubleshooting

| Symptom | Check |
| --- | --- |
| No LAN response | Correct protocol, camera IP, VLAN/client isolation and UDP firewall rule. |
| Indicator changes but SSH is unreachable | Local boot/listener state, Linux address/routes and MCU/HCC network readiness; do not equate SSH timeout with no SoC wake. |
| Wake works but Linux sleeps quickly | Add a timed hold once SSH is available; Wi-Fi prewake and Linux sleep inhibition are different controls. |
| Diagnostic query fails | Use the supplied reader's temporary Linux reply patch; inspect baseline version/reason failures and cleanup logs. |
| Cloud login fails | Account ownership, API region, password and MFA requirements. |
| Hold release reports busy or times out | Release is unconfirmed; retry on a reachable camera. Timed holds have camera-side expiry. |

No cold-start, reassociation, idle-power or long-term reliability guarantee is
made. See [current receiver and tooling](HI3861-LAN-PATCH.md).
