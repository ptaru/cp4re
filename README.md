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
| [`tools/probe.py`](tools/probe.py) | Read-only TCP probe for the documented service states. |

Deeper notes:

- [Platform guide](docs/PLATFORM.md) — hardware, software, services, and the division of work between the SoCs and MCUs.
- [Wake and sleep](docs/WAKE.md) — the LAN wake packet, cloud wake, evidence, and limits.
- [Firmware](docs/FIRMWARE.md) — the `.dav` container, acquisition protocol, app image format, build and installation procedure, and recovery.
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
| UDP **50161** | Demonstrated LAN wake receiver on the low-power networking side. |
| UDP 50160/50161 | Also observed in awake Linux `ezapp`; ownership depends on lifecycle. |
| TCP **2222** | Key-only SSH **only after** a custom app image is installed. Stock firmware does not expose it. |
| TCP 2323 | Historical raw root shell in early custom images; removed in the current design. |
| 127.0.0.1 TCP **7001** | Internal `ezapp` `da_info` diagnostic/control service. Loopback only. |
| TCP **8000** | Proprietary HCNetSDK listener, available only after an experimental in-RAM start. Not persisted. |
| UDP 37020 | SADP discovery, started with the experimental SDK helper. |
| Outbound TCP **8666** | Cloud bootstrap/authentication. |
| Outbound TCP **8820** | Cloud-assigned MQTT/DAS endpoint, held by the Wi-Fi MCU. |
| TCP 554 | Normally closed. No working RTSP server was established. |

A normal CP4 boot exposes **no LAN media service**. The camera sleeps; while
asleep, the Wi-Fi MCU can stay connected even though Linux and its services
are gone. Do not treat an unreachable shell as proof that the device is off.

### Wake and sleep

A single ordinary UDP datagram wakes the tested camera:

```text
destination port: 50161
payload (7 bytes): 50 03 00 00 bd 0c 2f
```

The receiver matches selected header bytes (selector `53` selects `50`, `03`,
`bd`, `/`; three bytes are ignored) and asserts logical **GPIO 2** into the
Novatek power domain. There is no cryptographic authentication, pairing
lookup, or encrypted-body parser on that matched branch. The payload is an
EZLink wake pattern supplied by the host library; it is not derived from the
device serial, but it may be model/firmware specific. The tool lets you
override it.

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
  helpers) and `cryptography`.
- `squashfs-tools` (`mksquashfs`, `unsquashfs`) only for building or unpacking
  app images.
- `tcpdump`/`editcap` only for capturing a bootstrap exchange.

The container, capture, and probe tools use only the Python standard library.
`get_firmware.py` and the cloud path of `wake.py` use `pyezvizapi`, which
depends on `cryptography`.

### Wake the camera

```sh
# LAN first, then the cloud preview fallback if needed.
python tools/wake.py \
  --host 192.168.1.50 \
  --serial ABC12345 \
  --account you@example.com

# LAN only; no account or cloud connection.
python tools/wake.py --host 192.168.1.50 --method lan

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
- **Treat captures and identities as private.** The device identity is stable
  even though it is not a password. The verification code is a credential.
- **No vendor affiliation.** This is independent research. EZVIZ has not
  endorsed or supported it.

---

## Findings and limits

Demonstrated on the tested unit:

- One UDP packet to 50161 wakes Linux with no cloud dependency at the moment
  of waking (three WAN-isolated tests; fresh uptime recorded).
- The cloud preview handshake wakes the camera as a fallback.
- The legacy device handshake, DAS redirect, and encrypted MQTT firmware query
  work; the cloud returned the actual installed package when an older version
  was advertised.
- The `.dav` container and app trailer can be parsed, rebuilt, and verified
  offline; an app-only modified image installed and booted through Local
  Upgrade with no trailer-version gate.
- A custom app image can provide key-only SSH on 2222 from `initrun.sh`.

Not established:

- Cold startup with no prior cloud contact, long-term sleep/wake reliability,
  or behavior after a factory reset.
- The same wake payload, update policy, secure-boot state, or shell route on
  other CP4 revisions, models, or firmware versions.
- A validated recovery path for a boot-looping modified image.
- A working RTSP server. The internal port-8000 SDK path and a read-only
  shared-memory media tap are research results, not packaged tools here.
- The exact PIR sensor wiring, the FM33 wake line, and the full inter-unit
  transport.

---

## Repository layout

```text
public/
├── README.md
├── LICENSE
├── requirements.txt
├── tools/
│   ├── wake.py
│   ├── get_firmware.py
│   ├── device_id.py
│   ├── dav.py
│   ├── make_firmware.py
│   └── probe.py
└── docs/
    ├── PLATFORM.md
    ├── WAKE.md
    ├── FIRMWARE.md
    └── PROTOCOL.md
```

## License

MIT. See [LICENSE](LICENSE). The firmware and protocol observations are
independent research; vendor firmware and trademarks remain the property of
their owners.
