# Standalone video server proof of concept

This repository contains a small static ARM HTTP server that reads
`ezdsp`'s existing MPEG-PS ring and serves **HEVC Main 1728×1080 with AAC
16 kHz mono**. It uses no SDK login, `da_info` command, port-8000 listener, or
executable patch. Its only write to `ezapp` is the RAM sleep-permission flag.

This is an exact-firmware proof of concept for **CP4 V5.3.3 build 251118**.
The stock capture processes must remain running; this does not replace
`ezapp` or `ezdsp`.

## Location and requirements

Development lives in [`tools/video_server/`](../tools/video_server/).

| File | Purpose |
| --- | --- |
| `cp4-video.c` | Camera-side ring reader, HTTP server, and sleep guardian. |
| `deploy.py` | Host-side wake, SD upload, and temporary launch. |
| `build.sh` | Cross-compile with Zig and static musl. |
| `test_ring.c` | Host checks for framing, queue limits, and wrapped history. |

The generated `cp4-video-arm` executable is ignored by Git; build it before
first deployment. No firmware image or private identity is included.

Running it requires Python 3, the local OpenSSH client, root key-only SSH on
camera TCP 2222, and a mounted SD card at `/mnt/mmc01`. Stock firmware has no
SSH service; see [app0 SSH](APP0-SSH.md). Playback requires an HEVC/AAC-capable
player such as `ffplay`. No cloud account or video password is used.

## Deploy and view

Run from this repository's root. Build once, then deploy and connect a player:

```sh
ZIG=/path/to/zig sh tools/video_server/build.sh
python3 tools/video_server/deploy.py --host 192.168.1.50 --protocol cp4
ffplay http://192.168.1.50:8080/live.ps
```

The address is an example; `--host` is required. Use `--protocol cp4` for the
patched receiver or `--protocol oem` (the default) for unmodified firmware.

`deploy.py` sends wake packets itself, waits for SSH and the SD card, checks
`ezapp`'s supported MD5, uploads a staged executable, and launches the server
in the background on the camera. It prints connection attempts, wake-packet
counts, readiness errors, and finally the stream URL, then **exits**. It is not
an ongoing host-side daemon. Allow roughly a minute for readiness retries;
each SSH deployment attempt has a 25-second timeout.

Options currently supported:

```sh
python3 tools/video_server/deploy.py \
  --host 192.168.1.50 --protocol cp4 --identity ~/.ssh/id_ed25519 --port 8080
```

SSH uses noninteractive key authentication. An encrypted identity must be
available through `ssh-agent`; the script does not prompt for its passphrase.
The HTTP endpoint has no authentication or encryption and listens on all
camera IPv4 interfaces by default. Keep it on the trusted LAN.

### OEM versus patched wake

The deployer imports the same packet profiles as `tools/wake.py`:

| Flag | Receiver | UDP port | Payload (hex) |
| --- | --- | --- | --- |
| `--protocol oem` (default) | Unmodified OEM receiver | 50161 | `50 03 00 00 bd 0c 2f` |
| `--protocol cp4` | Independent patched receiver | 50162 | `43 50 34 57 41 4b 45 01 50 03 bd 2f` |

It sends wake automatically; no separate wake invocation is needed. Wake sends
are limited to one every five seconds and stop once SSH answers, unless the
camera subsequently stays unreachable for a sustained interval. The selected
protocol and port are printed at startup. There is no cloud fallback.

Inspect the plan without reading the binary or opening any connections:

```sh
python3 tools/video_server/deploy.py --host 192.168.1.50 --protocol cp4 --dry-run
```

Do not combine deployment with a separate wake `--hold`: the video server
owns the sleep flag itself. See [wake protocols and troubleshooting](WAKE.md)
for cloud fallback and network diagnostics. Sending a wake packet is not
confirmation that Linux or SSH has become reachable.

## What remains running

The camera-side server gives the first viewer **ten seconds of startup hold**
to connect. After the first valid streaming request:

- One or more viewers keep the RAM sleep flag at `0`.
- Disconnecting the last viewer restores the flag to `1`, permitting sleep.
- Pending or incomplete HTTP connections do not extend the hold.

The server remains available while Linux is awake. Normal sleep stops Linux;
waking starts Linux again. The manual deployer does not modify boot hooks, so
manual deployments need relaunching after wake. The optional SD hook below
automatically launches the retained executable on each Linux boot.

A separate guardian checks the exact `ezapp` executable and reasserts the hold
every two seconds. Server exit, crash, or `SIGKILL` closes its command pipe,
and the guardian restores sleep permission independently. The server assumes
**exclusive ownership of the sleep flag**: do not run it alongside the public
wake tool's timed/indefinite hold worker or another hold manager. Killing the
guardian itself with `SIGKILL` cannot execute cleanup.

## Automatic start from SD

With an app0 image that invokes `/mnt/mmc01/cp4-hook.sh` after mounting the
card, the binary and hook can live entirely on SD. This was demonstrated on
the tested unit on 23–24 October 2026. No additional flash change is needed
if that hook invocation is already in the installed init script. See
[the app0 hook contract](APP0-SSH.md#optional-sd-boot-hook).

Build the server, then copy these files to the card root:

| Repository file | SD path |
| --- | --- |
| `tools/video_server/cp4-video-arm` | `/mnt/mmc01/cp4-video` |
| `examples/app0/cp4-hook-video.sh` | `/mnt/mmc01/cp4-hook.sh` |

Keep the card mounted while copying. Ensure the binary is executable (the
tested FAT mount supplies executable permissions). Merge with an existing
hook if it performs other tasks; do not overwrite unrelated startup logic.
The example checks the supported ezapp MD5, waits for the app process, avoids
a duplicate server using the PID file, and launches on port 8080. It writes
`/tmp/cp4-video.pid` and `/tmp/cp4-video.log`. App0 appends hook output to
`/mnt/mmc01/cp4-hook.log`. This is a one-shot launcher, not a crash supervisor.

Staging these files does not run the hook in the current boot. It takes effect
on the next Linux boot. For a manual test while awake, run
`sh /mnt/mmc01/cp4-hook.sh` over SSH only after releasing any separate hold
worker. Do not start the server alongside a timed/forever wake hold.

Once installed, a player can use wake followed by the retained HTTP service:

```sh
python3 tools/wake.py --host 192.168.1.50 --protocol cp4 --method lan --check-port 8080
ffplay http://192.168.1.50:8080/live.ps
```

For automatic wake and viewing through Scrypted, see
[the Scrypted plugin guide](SCRYPTED.md). The plugin sends UDP directly and
requires no host-side Python or SSH at runtime.

## Capture, inspect, and stop

Run capture commands on the receiving computer:

```sh
curl --max-time 20 http://192.168.1.50:8080/live.ps -o /tmp/cp4-live.ps
ffprobe /tmp/cp4-live.ps
ffmpeg -i http://192.168.1.50:8080/live.ps -c copy -f mpegts /tmp/cp4-live.ts
```

A time-limited `curl` normally exits with code 28; the saved stream is usable.
The output is MPEG-PS, not MP4 or RTSP. Browser video-tag playback is not
provided. Captures contain private camera footage.

The deployer writes these paths on the camera:

| Camera path | Purpose |
| --- | --- |
| `/mnt/mmc01/cp4-video` | Executable retained on SD. |
| `/tmp/cp4-video.pid` | Main server PID for this Linux boot. |
| `/tmp/cp4-video.log` | Startup, attachment, ring-wrap, and hold-release log. |

While the camera is reachable, inspect or stop it over SSH:

```sh
ssh -p 2222 root@192.168.1.50 'cat /tmp/cp4-video.log'
ssh -p 2222 root@192.168.1.50 'kill "$(cat /tmp/cp4-video.pid)"'
```

Manual camera-side launch also accepts a bind address:

```sh
/mnt/mmc01/cp4-video 8080 0.0.0.0 >/tmp/cp4-video.log 2>&1 &
echo $! >/tmp/cp4-video.pid
```

## Reader design

Media and control segments are attached with `SHM_RDONLY`. The reader never
changes a vendor cursor, semaphore, or media payload.

| Item | Exact-firmware value |
| --- | --- |
| Media SysV key | `0x650100c1` |
| Control SysV key | `0x640100c1` |
| Ring-0 payload start | Media arena `+8` |
| Ring capacity | `0x400000` bytes (4 MiB) |
| Ring-0 descriptor | Control `+0x8c370` |
| Write-cursor field | Descriptor `+8` |
| RAM sleep-permission word | `6753236` (`0x670bd4`) |
| Required `ezapp` MD5 | `8a4f3c1bf4f850318a17fae01305c20b` |

The extra 16 KiB allocation and allocation header are not stream capacity.
The server follows the published cursor with its own read position, handling
wrap without participating in the vendor consumer protocol.

Up to eight viewers have independent bounded 1 MiB output queues. A full
queue or five seconds without output progress disconnects that viewer rather
than blocking the producer. Incomplete HTTP requests also time out.

New viewers receive the latest header-bearing PS pack from a 768 KiB history
snapshot, including the program map, HEVC parameters, and keyframe. This can
replay approximately one GOP before catching up. If no suitable history
exists, the reader waits for a new header-bearing pack.

Missing shared segments are retried. A polling gap longer than two seconds
causes viewer disconnection to avoid joining uncertain stream data. An
in-place `ezdsp` restart while already attached is not recovered automatically.

## Build and checks

From this repository root:

```sh
ZIG=/path/to/zig sh tools/video_server/build.sh
cc -Wall -Wextra -fsanitize=address,undefined -g \
  tools/video_server/test_ring.c -o /tmp/cp4-test-ring
/tmp/cp4-test-ring
```

The static build targets `arm-linux-musleabihf`, Cortex-A9, hard float. The
server uses `poll`; the Go runtime's epoll initialization failed on the tested
kernel. Host checks cover headers split across reads, queue compaction and
limits, wrapped history, and choosing the newest header-bearing pack. Deployer
checks verify both packet profiles, readiness retries without extra wake
packets, dry-run behavior, and immediate authentication failures:

```sh
python3 -m unittest discover -s tests -p 'test_video_deploy.py' -v
```

## Validation and limits

Live tests on 22 October 2026 demonstrated:

- Multiple simultaneous viewers; disconnecting one did not stop another.
- Last-viewer release with the live sleep flag read back as `1`.
- A **61.09-second, 8,027,720-byte steady-state capture**, joined after 36
  seconds, with HEVC 1728×1080 and AAC 16 kHz mono. Full software decoding
  produced **zero errors**, with **two ring wraps** logged during the run.
- Restoration from flag `0` to `1` after main-server `SIGKILL`, while another
  viewer was still connected.

Captures begun near boot showed a short decoding disruption around source
PTS 30 seconds in two runs. The cause remains unresolved; its repeating source
timestamp suggests a startup transition, but that is an inference. The clean
steady-state test does not establish flawless playback immediately after boot.

This is a research service with exact-firmware offsets, optional SD boot
integration, no HTTP access control, and no long-term reliability claim.
Scrypted browser snapshots, live playback, and HomeKit Accessory Mode / protocol
V3 playback were subsequently demonstrated; see [Scrypted](SCRYPTED.md).

## Troubleshooting

| Symptom | Meaning / next check |
| --- | --- |
| Terminal appears inactive | Current deployer prints attempts immediately. Older copies hid SSH output until success or timeout; use the current deployer in `tools/video_server/`. |
| Repeated SSH timeouts | Check camera address, correct OEM/patched wake protocol, network isolation and SSH availability. The deployer wakes automatically using the selected `--protocol`; confirm the flag matches the installed receiver. |
| `Permission denied` | Load the owner key with `ssh-add`, or pass `--identity`; verify root SSH access on 2222. |
| Host-key verification error | Verify the camera identity and resolve the OpenSSH known-host entry before retrying. |
| SD or `ezapp` not ready | These conditions are retried while Linux starts. Confirm the SD card is mounted if retries expire. |
| Deployment code 20 | Unsupported `ezapp` hash; fixed memory offsets must not be applied to another binary. |
| Deployment code 21 | Invalid saved PID file; inspect `/tmp/cp4-video.pid`. |
| Deployment code 23 or 24 | Server exited or did not become ready; inspect `/tmp/cp4-video.log`. |
| URL stops after idle / wake | Normal sleep restarts Linux. Install the SD boot hook or redeploy manually; the binary alone does not provide boot persistence. |
| Player cannot identify codecs | Use an HEVC/AAC MPEG-PS-capable player and inspect the log; browser playback is not implemented. |
| Brief corruption shortly after boot | Known disruption near source PTS 30 seconds; compare a capture joined after startup. |

Releasing the sleep flag permits sleep rather than forcing it immediately.
An unreachable camera after release is consistent with sleep, but does not by
itself prove the flag was restored.
