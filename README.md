# EZVIZ CP4 Reverse Engineering

Independent research notes and tooling for the **EZVIZ CP4 peephole camera**
(model `CS-CP4-R100-6E2WPFBS`). The project documents how the camera wakes,
authenticates to the EZVIZ cloud, obtains its firmware, and accepts a modified
app image through the local upgrade path.

Everything here was established on one personally owned unit running firmware
**V5.3.3 build 251118**. Treat every address, payload, port, and version
observation as a result for that unit and that firmware, not as a product
specification. Other models and firmware revisions may differ.

> **Scope.** Use this only on devices you own or are explicitly authorized to
> test. The tools can impersonate a device identity, interrupt its cloud
> session, and build firmware images that may brick a device. Keep cameras on
> a trusted LAN, disable UPnP, and never expose the shell or SDK ports to the
> internet. See [Safety](#safety).

This repository contains no device serials, MAC addresses, account
credentials, verification codes, device identities, captures, private keys, or
firmware images. The tools take those values from you at run time.

---

## What is here

| Tool | Purpose |
| --- | --- |
| [`tools/wake.py`](tools/wake.py) | Combined LAN + cloud wake. Sends the proven UDP wake and falls back to the EZVIZ cloud preview handshake. |
| [`tools/get_firmware.py`](tools/get_firmware.py) | Authenticate as a device, query the cloud firmware service, download the package, and verify its size and MD5. |
| [`tools/device_id.py`](tools/device_id.py) | Extract the 32-byte device identity from a bootstrap packet capture for `get_firmware.py`. |
| [`tools/dav.py`](tools/dav.py) | Inspect, verify, unpack, diff, and repack the `SWKH` `.dav` firmware container, including the `app.img` trailer. |
| [`tools/make_firmware.py`](tools/make_firmware.py) | Build a modified `.dav`: SquashFS an app filesystem, refresh the app trailer, replace `app.img`, and verify. |
| [`tools/hi3861_ota.py`](tools/hi3861_ota.py) | Validate, unpack, and rebuild the CP4 Hi3861L MCU OTA. The builder is pinned to the documented 2.139 image and performs no device I/O. |
| [`tools/hi3861_wake/`](tools/hi3861_wake/) | Current receiver C/assembly and exact compiled code plan. |
| [`tools/hi3861_flash/`](tools/hi3861_flash/) | Candidate-pinned Go launcher, preparation probe and explicit MCU updater entry point. |
| [`tools/hi3861_diag/`](tools/hi3861_diag/) | Read receiver snapshots with a temporary Linux RAM reply patch. |
| [`tools/video_server/`](tools/video_server/) | Standalone HEVC/AAC HTTP server with client-driven sleep holds; deploy with OEM or patched CP4 LAN wake. |
| [`plugins/cp4-camera/`](plugins/cp4-camera/) | Scrypted on-demand UDP wake, live video, snapshots, and HomeKit integration. |
| [`tools/probe.py`](tools/probe.py) | Read-only TCP probe for the documented service states. |

The wake and firmware network tools print progress to stderr and a JSON summary to stdout, so
`python tools/wake.py ... | jq .` works. Use `--json-out` to save a report.

The [`examples/app0/`](examples/app0/) directory contains the TinySSH
non-multiuser patch, `inetd.conf`, initrun additions, and a build template for
the key-only SSH setup. See [app0 SSH](docs/APP0-SSH.md).

Deeper notes:

- [Platform guide](docs/PLATFORM.md) — hardware, software, services, and the division of work between the SoCs and MCUs.
- [Wake and sleep](docs/WAKE.md) — OEM and patched LAN profiles, cloud wake, timed/indefinite holds and troubleshooting.
- [Standalone video server PoC](docs/VIDEO-SERVER.md) — build, deployment, HEVC/AAC playback, client-driven sleep holds, validation and troubleshooting.
- [Scrypted camera plugin](docs/SCRYPTED.md) — SD autostart prerequisites, build/deploy, WebRTC viewing, HomeKit Accessory Mode / V3, and sleep troubleshooting.
- [Firmware](docs/FIRMWARE.md) — the `.dav` container, acquisition protocol, app image format, build and installation procedure, and recovery.
- [Hi3861L LAN-wake patch](docs/HI3861-LAN-PATCH.md) — the independent receiver, Wi-Fi prewake, exact image build, pinned flash launcher and diagnostics.
- [app0 SSH](docs/APP0-SSH.md) — patching TinySSH for the non-multiuser kernel, the app0 layout, and the initrun changes.
- [Protocol](docs/PROTOCOL.md) — the legacy channel-99 authentication and firmware-query protocol.

---

## Platform details

### Hardware

| Item | Observation |
| --- | --- |
| Product | EZVIZ CP4 peephole camera, model `CS-CP4-R100-6E2WPFBS`. |
| Physical arrangement | Door-mounted camera/PIR/audio unit plus a removable touchscreen panel. The inter-unit wiring is only partly mapped. |
| Main SoC | Novatek **NA51055** platform identification (device-tree `novatek,na51055`); 32-bit ARM EABI5, hard-float, uClibc/Buildroot userspace. |
| OS | Linux **4.19.91**, plus a separate `rtos.bin` boot/power image on the main SoC. |
| RAM | Kernel reports roughly 141 MB available; a separate **110 MiB** media zone spans `0x09200000`–`0x10000000`, consistent with a 256 MiB layout (fitted DRAM not physically confirmed). |
| Application flash | `app0` is a **6 MiB** partition (`mtd10`). |
| Wi-Fi controller | **Hi3861LV100 / Hi3861L**, RISC-V, LiteOS/lwIP. Owns low-power networking, MQTT/DAS, and the LAN wake receiver. |
| Auxiliary MCUs | FM33 **L01** and **L02**. `/dev/ttyS2` pairs with L01 in the door unit; `/dev/ttyS1` pairs with L02 in the touchscreen. L01 handles PIR sensitivity/keep time; L02 handles battery/status telemetry. |
| Sensor | Loaded driver `nvt_sen_f53` (software identification, not a confirmed chip marking). |
| Video | Hardware HEVC. Observed clean stream: **1728×1080 HEVC Main**. |
| Audio | Observed live output: **AAC, 16 kHz, mono**. |
| Display | 4.3-inch LCD. |
| Storage | Removable SD card at `/mnt/mmc01`; FAT32 was used for the tested upgrade workflow. |

### Services and ports

| Endpoint | Meaning |
| --- | --- |
| UDP **50162** | Patched CP4 wake receiver, fixed 12-byte protocol with Wi-Fi prewake. |
| UDP **50161** | Unmodified firmware wake profile. |
| UDP 50160/50161 | Also observed in awake Linux `ezapp`; ownership depends on lifecycle. |
| TCP **2222** | Key-only SSH **only after** a custom app image is installed. Stock firmware does not expose it. |
| TCP 2323 | Historical raw root shell in early custom images; removed in the current design. |
| 127.0.0.1 TCP **7001** | Internal `ezapp` `da_info` diagnostic/control service. Loopback only. |
| TCP **8080** | Optional SD video server: HEVC/AAC over HTTP MPEG-PS; active viewers hold Linux awake. |
| TCP **8000** | Proprietary HCNetSDK listener, available only after an experimental in-RAM start. Not persisted. |
| UDP 37020 | SADP discovery, started with the experimental SDK helper. |
| Outbound TCP **8666** | Cloud bootstrap/authentication. |
| Outbound TCP **8820** | Cloud-assigned MQTT/DAS endpoint, held by the Wi-Fi MCU. |
| TCP 554 | Normally closed. No working RTSP server was established. |

A normal CP4 boot exposes **no LAN media service**. The camera sleeps; while
asleep, the Wi-Fi MCU can stay connected even though Linux and its services
are gone. Do not treat an unreachable shell as proof that the device is off.

### Wake and sleep

The [patched CP4 receiver](docs/HI3861-LAN-PATCH.md) uses one ordinary UDP packet:

```text
destination port: 50162
payload (12 bytes): 43 50 34 57 41 4b 45 01 50 03 bd 2f
```

It requests a 20-second OEM Wi-Fi listening boost, sets reason 4 and asserts
logical GPIO 2. It starts independently of Linux pattern setup, has no cumulative
wake limit or application packet store, and consumes requests within its one-second
assertion cooldown. The payload identifies the protocol; it is not authentication.
Use `--protocol cp4`. Unmodified firmware uses the default `--protocol oem`.

The cloud also wakes the camera. In controlled tests the reliable trigger was
the cloud **live-view (VTM) preview handshake**, even when the connection was
closed immediately after `StreamInfoRsp`. The
`EnergySaving/SpeedUpListeningFrequency` IoT action alone was **not** enough in
the first test, so `wake.py` defaults to the preview path.

Observed wake-from-sleep restarts Linux: uptime resets and boot scripts run
again. Cold startup with no prior cloud contact has not been verified.

### Firmware container

The `.dav` package is an `SWKH` container:

- A **460-byte header** is XOR-obfuscated with a fixed 16-byte key.
- Nine entries (for CP4 V5.3.3) name the components and store their offset,
  size, and a 32-bit **additive byte sum**.
- Components are plaintext and packed back to back. The official CP4 package
  is **not encrypted and carries no signature** on the app-side local-upgrade
  path; `secure_flag` is `0xff` (the security-firmware value would be `0x87`).
- `app.img` is a **LZO SquashFS** image padded to 4096 bytes, followed by a
  **128-byte `SWKH` trailer** whose version string is identification only.
- The bootloader's real secure-boot stack (`SCB_STATUS=SCB_EN`, AES-CBC + RSA
  secure partitions) is dormant on the tested unit because `SCB_STATUS` is not
  defined and the package does not use it.

Local Upgrade reads `/mnt/mmc01/ezviz.dav` from the SD card. On the tested
unit it accepted an app-only image with no trailer-version gate and booted it.
This is an observation about one unit, not a promise about other hardware.

### Cloud protocol

The proven device route uses the legacy channel-99 authentication:

1. TCP 8666 to the regional LBS/bootstrap host.
2. AUTH-I/AUTH-II with the serial and the device verification code, then
   AUTH-III/AUTH-IV using the existing 32-byte device identity.
3. Encrypted DAS redirection to the MQTT broker.
4. MQTT 3.1.1 as the device, encrypted application envelopes.
5. Firmware query command **10339**; response on `/serial/1100/10340`.
6. The response contains `FileInfo Name`, `CheckSum`, `Version`, `Size`, and
   an HTTPS upgrade server. The download is verified against the MD5 and size.

Existing-device authentication rotates the device's master/session keys. The
physical camera may briefly reconnect or reauthenticate, and its cloud session
can be disrupted. Read [Protocol](docs/PROTOCOL.md) before using
`get_firmware.py`.

---

## Quick start

### Requirements

- Python **3.12+** (`pyezvizapi` requires it).
- `pip install -r requirements.txt` for `pyezvizapi` (cloud wake, protocol
  helpers), `cryptography`, and `capstone` (Hi3861 defense emulation).
- A C compiler (`cc`/`clang`/`gcc`) only for `hi3861_ota.py patch`, which
  compiles the bundled public-domain LZMA SDK helpers.
- `squashfs-tools` (`mksquashfs`, `unsquashfs`) only for building or unpacking
  app images.
- `tcpdump`/`editcap` only for capturing a bootstrap exchange.

The container, capture, probe, and `hi3861_ota.py` tools use only the Python
standard library. `get_firmware.py` and the cloud path of `wake.py` use
`pyezvizapi`, which depends on `cryptography`. Optional receiver recompilation
checks use Zig 0.17 and `pyelftools`; ARM flash/diagnostic helpers need Go.

### Wake the camera

```sh
# Patched LAN protocol first, then cloud preview fallback if needed.
python tools/wake.py --protocol cp4 \
  --host 192.168.1.50 \
  --serial ABC12345 \
  --account you@example.com

# LAN only; no account or cloud connection.
python tools/wake.py --host 192.168.1.50 --protocol cp4 --method lan

# Check what is reachable without waking anything.
python tools/probe.py --host 192.168.1.50
```

The account password is read from `EZVIZ_PASSWORD` or a hidden prompt. The
serial and host have no defaults. `--check-port` changes what "awake" means
(the default checks 2222 and 2323, which only exist on custom images).

### Get the firmware for a device

First obtain the device identity. The device sends it in the clear during its
own legacy bootstrap exchange, so a short packet capture is enough:

```sh
# On a router or span port, while the camera reconnects:
sudo tcpdump -i br-lan -w cam-boot.pcap 'host 192.168.1.50 and tcp port 8666'

# Power-cycle or re-dock the camera, then:
python tools/device_id.py cam-boot.pcap --write device-id.json
```

Then query and download:

```sh
python tools/get_firmware.py \
  --serial ABC12345 \
  --model CS-CP4-R100-6E2WPFBS \
  --device-id-file device-id.json \
  --state session.json \
  --output firmware.dav
```

The verification code is shown on the device (the touchscreen, for the CP4).
It is read from `EZVIZ_DEVICE_CODE` or a hidden prompt and is never stored.
`--state` saves the rotated master/session keys (mode 0600) so later runs can
refresh instead of re-authenticating. `--register` asks the cloud to allocate a
new identity instead of using a captured one; it is more disruptive and is not
the recommended path.

If the cloud says there is no update, advertise an older version:

```sh
python tools/get_firmware.py ... --advertise-version 'V0.0.0 build 000000'
```

### Inspect and build firmware

```sh
# Inspect and verify the official package.
python tools/dav.py info official.dav
python tools/dav.py verify official.dav --roundtrip

# Extract all components, then the app SquashFS.
python tools/dav.py unpack official.dav unpacked/
python tools/dav.py unpack-app unpacked/app.img app-root/

# Modify app-root/, then rebuild app.img and the container.
python tools/make_firmware.py \
  --base official.dav \
  --app-root app-root \
  --version 5.3.16 \
  --out custom.dav

# A recovery package keeps the original app contents.
python tools/make_firmware.py \
  --base official.dav --recovery --version 5.3.17 --out recovery.dav

# Compare two images component by component.
python tools/dav.py diff official.dav custom.dav
```

Read [Firmware](docs/FIRMWARE.md) before installing anything. Keep the
official package and a recovery image on separate SD cards, do not touch
`app1`, `uboot`, OTP/`enc`, or `/devinfo`, and remember that recovery is not
validated.

### Inspect and patch the MCU image

The Hi3861L application OTA is a file inside the extracted app filesystem
(`/dav/Hi3861L_ezviz_sdk_ota.bin` on the camera):

```sh
python tools/hi3861_ota.py info app-root/Hi3861L_ezviz_sdk_ota.bin
python tools/hi3861_ota.py unpack app-root/Hi3861L_ezviz_sdk_ota.bin \
  --out hi3861-unpacked
python tools/hi3861_ota.py patch app-root/Hi3861L_ezviz_sdk_ota.bin \
  --out hi3861-patch
```

`hi3861_ota.py patch` needs a C compiler and refuses a different source or
result. It writes `Hi3861L_CP4_independent_wake_ota.bin`, `kernel.bin` and a
manifest; it does not install them. Receiver sources and exact code bytes are
in `tools/hi3861_wake/`; optional Zig recompilation verifies their equality.

The source-only Go flash launcher and diagnostic reader are in
`tools/hi3861_flash/` and `tools/hi3861_diag/`. The reader temporarily patches
Linux's reply switch for the MCU snapshot, validates HCC before/after, and
restores the instructions. Read the [build, preparation, explicit flash and
diagnostic instructions](docs/HI3861-LAN-PATCH.md#tooling) before using either.

---

## Safety

- **Only your own device.** `get_firmware.py` authenticates as the device and
  rotates its session keys. It can briefly disrupt the camera's cloud session.
- **LAN only.** Do not forward SSH 2222, the historical raw 2323, the SDK 8000,
  or any debug port to the internet. Disable UPnP. Restrict access at the
  router.
- **Firmware is dangerous.** A wrong app image can make the UI or shell
  unreachable. Keep the untouched official package and a recovery package on
  separate cards. Recovery through the touchscreen, automatic SD recovery,
  and bootloader slot fallback were not validated on the tested unit.
- **Never write OTP or `cipher_otp.bin`.** OTP writes are irreversible.
- **MCU flashing is not app0 flashing.** The Hi3861L patch has no validated
  recovery path on the tested unit and can leave the Wi-Fi MCU unbootable.
  Read [Hi3861L LAN-wake patch](docs/HI3861-LAN-PATCH.md) before considering it.
- **Treat captures and identities as private.** The device identity is stable
  even though it is not a password. The verification code is a credential.
- **The SSH image is a credential.** If you bake a host key or authorized key
  into app0, keep that image private and restrict 2222 to your LAN. See
  [app0 SSH](docs/APP0-SSH.md).
- **No vendor affiliation.** This is independent research. EZVIZ has not
  endorsed or supported it.

---

## Findings and limits

Demonstrated on the tested unit:

- The patched receiver accepts one UDP 50162 packet and wakes Linux. A
  controlled post-install wake returned SSH in about 5.6 seconds; long-term
  reliability and idle power remain unmeasured.
- The cloud preview handshake wakes the camera as a fallback.
- The legacy device handshake, DAS redirect, and encrypted MQTT firmware query
  work; the cloud returned the actual installed package when an older version
  was advertised.
- The `.dav` container and app trailer can be parsed, rebuilt, and verified
  offline; an app-only modified image installed and booted through Local
  Upgrade with no trailer-version gate.
- A custom app image can provide key-only SSH on 2222 from `initrun.sh`. The
  patch, app0 layout, and init script are documented in
  [app0 SSH](docs/APP0-SSH.md).
- The current MCU receiver and Wi-Fi prewake extension installed and booted;
  snapshots and supported HCC calls passed with Linux instruction restoration
  verified. See the [receiver reference](docs/HI3861-LAN-PATCH.md).

- SD boot-hook video startup, Scrypted snapshots and live viewing, and Apple
  Home playback with HomeKit Accessory Mode / protocol V3. See
  [the Scrypted guide](docs/SCRYPTED.md) for setup and observed limits.

Not established:

- Cold startup with no prior cloud contact, long-term sleep/wake reliability,
  or behavior after a factory reset.
- The same wake payload, update policy, secure-boot state, or shell route on
  other CP4 revisions, models, or firmware versions.
- A validated recovery path for a boot-looping modified image.
- A camera-native RTSP server. The [standalone video PoC](docs/VIDEO-SERVER.md)
  serves HTTP MPEG-PS; Scrypted can rebroadcast it as RTSP on the host.
  The internal port-8000 SDK path remains experimental.
- The exact PIR sensor wiring, the FM33 wake line, and the full inter-unit
  transport.
- Hi3861L flash readback, measured prewake expiry/power, long-term patched
  behavior or recovery from an unbootable Wi-Fi MCU.

---

## Repository layout

```text
public/
├── README.md
├── LICENSE
├── requirements.txt
├── tools/
│   ├── wake.py
│   ├── wake_hold.py
│   ├── get_firmware.py
│   ├── device_id.py
│   ├── dav.py
│   ├── make_firmware.py
│   ├── hi3861_ota.py
│   ├── hi3861_wake/       # Receiver sources and pinned code plan
│   ├── hi3861_flash/      # Source-only pinned MCU launcher
│   ├── hi3861_diag/       # Source-only diagnostic reader
│   ├── hi3861_lzma/
│   │   ├── encode.c
│   │   ├── decode.c
│   │   ├── sdk19/
│   │   ├── sdk920/
│   │   ├── PROVENANCE.json
│   │   └── README.md
│   └── probe.py
├── plugins/
│   └── cp4-camera/       # On-demand Scrypted integration
├── examples/
│   └── app0/
│       ├── cp4-hook-video.sh
│       ├── inetd.conf
│       ├── initrun-additions.sh
│       ├── tinyssh-cp4-nonmultiuser.patch
│       └── build-tinyssh.sh
└── docs/
    ├── PLATFORM.md
    ├── WAKE.md
    ├── FIRMWARE.md
    ├── HI3861-LAN-PATCH.md
    ├── VIDEO-SERVER.md
    ├── SCRYPTED.md
    ├── APP0-SSH.md
    └── PROTOCOL.md
```

## License

MIT. See [LICENSE](LICENSE). The firmware and protocol observations are
independent research; vendor firmware and trademarks remain the property of
their owners. The LZMA SDK subset under `tools/hi3861_lzma/sdk19/` and
`tools/hi3861_lzma/sdk920/` is public domain and retains its upstream headers;
see [PROVENANCE.json](tools/hi3861_lzma/PROVENANCE.json).
