# Hi3861L LAN wake receiver

This patch replaces the CP4 Hi3861L application’s UDP receiver with a fixed
wake protocol, an application-queue-free receive loop and a temporary Wi-Fi
listening boost. It targets the exact **2.139** image identified below.

## Protocol and behavior

| Item | Value |
| --- | --- |
| Transport | IPv4 UDP unicast, destination port **50162** |
| Payload | `43503457414b45015003bd2f` — exactly 12 bytes |
| Meaning | ASCII `CP4WAKE`, version byte 1, fixed identifier bytes |
| Accepted wake | Request OEM 20-second prewake, set reason 4, assert logical GPIO 2 |
| Cooldown | One second between assertions; matching packets inside it are consumed |
| Packet retention | No application packet store or Linux drain requirement |
| Replies | None over UDP |
| Startup | MCU application initialization, independent of Linux wake-pattern setup |
| Recovery | Retry socket/filter failures and observed association changes |
| Acknowledgement | Existing GPIO-7/host-ready paths clear the wake output |

The receiver checks exact bytes and length, receiving into a 13-byte buffer to
reject longer datagrams with a matching prefix. It imposes no cumulative packet
limit. The underlying network stack still has finite queues and memory. The
payload is an identifier, not authentication; restrict access to the trusted LAN.

The task uses the existing creation wrapper: priority 25 and a 3072-byte stack.
It polls `select` with a 250 ms timeout, refreshes the receive filter every ten
seconds, and recreates the socket after observed network-state changes. These
intervals require power and long-idle testing; they are not battery guarantees.
The normal host-sleep filter is also changed to 50162. The default OEM port is
not the patched MCU receiver’s wake port.

### Wi-Fi prewake

Before GPIO assertion, the receiver calls OEM helper `0x41e5e2(20)`. This is the
routine used by `EnergySaving/SpeedUpListeningFrequency`. It adjusts Wi-Fi PM
settings, records an expiry and creates/reuses the `ez_prewake` task. Subsequent
accepted wakes refresh the window. No MQTT request or fixed pre-GPIO delay is
required.

The expiry task normally restores sleep/listening settings when the host is
asleep. The helper’s return value does not indicate task-creation success, so
the receiver checks the task handle. If absent, it clears the prewake flag and,
when the host remains asleep, restores the normal `(1,1000)` PM interval.
GPIO-helper calls are counted as assertions; they do not measure electrical
edges or prove that Linux subsequently became reachable.

## Image identity and layout

| Item | SHA-256 / size |
| --- | --- |
| Source OTA | `e01c4867892f611326c4935abdcfb1d9a6c19629f2953b0cd441fb36578596c2` |
| Source expanded kernel | `ca5e1a57ff4b4f056f1a279fc0e902605126fd9651444ea9f84096d74bf6d788` |
| Patched OTA | `951c3e569cd794821637263e805105c65b2e576cdd006f9028c4e3aea49679fb` |
| Patched expanded kernel | `8d97d46ac40ed36fbf2dbfce593ab12998664a4e80e4756cb6fae960d764accd` |
| OTA / expanded size | 552,608 / 911,792 bytes |
| Compressed stream | 551,636 bytes |
| MCU version | 2.139, unchanged |

The source is `Hi3861L_ezviz_sdk_ota.bin` inside the camera’s extracted app0
filesystem, `/dav/Hi3861L_ezviz_sdk_ota.bin` on the running camera. This OTA is
separate from the `.dav` camera package and flashboot OTA.

| MCU address | Change |
| --- | --- |
| `0x4209ba–0x420bdf` | Reclaimed receiver/search/defense region; 526/550 bytes used |
| `0x420be0–0x420ef3` | Replacement receiver and helpers; 784/788 bytes used |
| `0x41f0d4` | Startup call to bootstrap, preserving GPIO ISR initialization |
| `0x41dbfa` | Host-sleep receive-filter port immediate, 50162 |
| `0x4ce3c0` | HCC command-0x12 jump-table entry to snapshot bridge |

Kernel addresses equal expanded file offsets plus `0x40d000`. The image
builder allows changes only in these regions and the inner hash. It regenerates
outer lengths/hashes and padding. Bootloader, NV, initialization-copy offsets,
version and total expanded size remain unchanged.

The encoder is LZMA SDK 19.00: `lc=2, lp=2, pb=2, dict=65536`, 128 fast bytes,
normal algorithm, BT4, known-size 13-byte header without an end marker. The
builder first reproduces the source stream exactly and uses an independent SDK
9.20 decoder with 1/13/64/4096-byte chunks. Hash-only wrappers are regenerated;
this does not satisfy an RSA/ECC-enforced verification configuration. No eFuse
or bootloader changes are made.

## Tooling

No vendor firmware image, account data, keys, device captures or prebuilt ARM
executables are distributed. Obtain the matching source OTA from your own
camera package.

### Build the OTA offline

Python 3.10+ and a C compiler are required. From the repository root:

```sh
python tools/dav.py unpack firmware.dav unpacked/
python tools/dav.py unpack-app unpacked/app.img app-root/
python tools/hi3861_ota.py info app-root/Hi3861L_ezviz_sdk_ota.bin
python tools/hi3861_ota.py patch app-root/Hi3861L_ezviz_sdk_ota.bin \
  --out hi3861-patch
```

The output is `hi3861-patch/Hi3861L_CP4_independent_wake_ota.bin`, its expanded
`kernel.bin` and a manifest. The builder refuses a different source or result,
checks the allowed differences, hashes and decompression round trip, and makes
no network connection.

[`tools/hi3861_wake/`](../tools/hi3861_wake/) contains the C/assembly source,
linker bounds and the exact compiled code/hook plan used by the builder.
To independently rebuild the instruction sections using Zig 0.17 and pyelftools:

```sh
python -m pip install pyelftools
python tools/hi3861_wake/build_code.py --zig /path/to/zig
```

This checks exact equality with the packaged instruction sections; it does not
install firmware. The pinned compiled sections let ordinary OTA builds use a C
compiler without requiring a RISC-V compiler.

### Prepare and install

The source-only Go launcher uses key-only root SSH on TCP 2222 and the exact
app0 `ezapp`/`liblpwwifi.so` identities checked in its source. Go is required to
cross-build the ARM helper. Keep continuous external power and a working SD
card. Do not run diagnostics or another hold manager during preparation/update.

```sh
sh tools/hi3861_flash/build.sh
python tools/hi3861_flash/prepare.py --host CAMERA_IP \
  --image hi3861-patch/Hi3861L_CP4_independent_wake_ota.bin
```

Preparation sends one OEM wake, stages and verifies the helper/candidate under
`/mnt/mmc01/hi3861-wake/`, uses a bounded preparation hold, and runs non-flashing
file/mapping, pthread, version and pinned-file-opening probes. It saves a local
deployment manifest and probe log. Use `--protocol cp4` if the camera already
has the replacement receiver, or `--no-wake` after a physical/cloud wake.
`--identity PATH` selects an SSH key. There is no cloud login in this launcher.

Local readiness verification makes no connection or updater call:

```sh
python tools/hi3861_flash/flash.py --host CAMERA_IP \
  --image hi3861-patch/Hi3861L_CP4_independent_wake_ota.bin
```

The following explicitly invokes the MCU updater:

```sh
python tools/hi3861_flash/flash.py --host CAMERA_IP \
  --image hi3861-patch/Hi3861L_CP4_independent_wake_ota.bin --execute
```

Apply the same `--protocol`/`--no-wake` selection as preparation. Ensure the
probe has finished and released its lock before execution. The launcher repeats
checks and calls `ezapp:0x2fd9c(pinned_fd_path,10)` on a fresh pthread inside the
existing HCC owner. It pins the open image inode through `/proc/HELPER_PID/fd/N`.
The OEM wrapper owns transfer retries, verification, delay and host reboot.
No external competing HCC library instance is started.

The driver records intent before sending one launch request and never retries
an ambiguous launch. Exit 3 after launch means boot success is still unverified.
The durable SD `flash-TIMESTAMP.log`, console log and persistent `launcher.lock`
preserve the operation state. Do not remove the lock or flash again to verify
success. A dropped connection or version 2.139 alone is not success evidence.
Check a fresh functional boot, diagnostics and controlled sleep/wake instead.

Normal startup skips a same-version MCU file, so replacing the bundled file in
app0 alone does not install this OTA. Keep version 2.139: changing the major
version can select flashboot rather than the kernel. Software rollback requires
working MCU HCC; unbootable-MCU recovery via UART/JTAG is not established.

### Diagnostics

The MCU provides an 80-byte, version-1 snapshot through HCC command `0x12`.
It reports generation, online/socket/filter readiness, receive/invalid/suppressed
and assertion counters, socket/filter errors, timestamps and retry state.
It does not drain packets or change receiver configuration.

The Linux library drops command-0x12 replies by default. The reader temporarily
patches its mapped reply dispatcher, requires supported version/reason queries
to pass first, requests one snapshot, checks supported queries afterward, then
restores and reads back both original instructions. Disk and MCU flash remain
unchanged. An in-flight timed-out worker retains scratch/patch state rather
than invalidating active code; Linux reboot clears the temporary patch.

```sh
sh tools/hi3861_diag/build.sh
python tools/hi3861_diag/read_status.py --host CAMERA_IP --label receiver
```

The reader sends one new-protocol wake and uses/releases a bounded hold. Use
`--no-wake` on an already awake camera. Baseline failure skips the patch/query;
wrong magic/version or failed calls are errors. `--check-only --no-wake` checks
files and addresses without HCC calls or Linux text changes. Do not run during
an update. Logs and snapshots remain local/private and are git-ignored.

## Validation and limits

The exact image has been installed and returned with working Wi-Fi/SSH.
Diagnostic snapshots and supported HCC queries passed before/after the query,
with Linux patch restoration verified. One controlled wake after waiting beyond
the nominal boost window restored SSH in approximately 5.6 seconds with fresh
Linux uptime. This is not a latency comparison or proof of long-term reliability.

Compiled-code QEMU checks covered packet matching, cooldown/wrap, reconnect and
socket/filter retries, prewake call order/duration, task-creation failure
fallback, snapshot bridge and a modeled stack guard. Actual proprietary ROM
stack depth, expiry/power behavior, reassociation, long idle and MCU cold-start
behavior remain unmeasured. The status snapshot does not expose the prewake
handle/deadline or electrical GPIO state. No installed-image readback or final
updater return was obtained.

See [Wake and sleep](WAKE.md), [Platform](PLATFORM.md) and [Firmware](FIRMWARE.md).
