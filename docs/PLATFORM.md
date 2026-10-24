# CP4 platform guide

Practical reference for the tested **EZVIZ CP4 peephole camera**
(`CS-CP4-R100-6E2WPFBS`), firmware **V5.3.3 build 251118**. It covers the
hardware blocks, the software layout, the service surface, and what was
actually demonstrated. Values are observations from one unit, not a product
specification.

## Hardware

| Item | Observation |
| --- | --- |
| Main SoC | Novatek **NA51055** (`novatek,na51055` in the device tree). 32-bit ARM EABI5, hard-float. |
| Main OS | Linux **4.19.91**, Buildroot/uClibc userspace. A separate `rtos.bin` image runs on the same SoC for boot/power coordination. |
| Wi-Fi MCU | **Hi3861LV100 / Hi3861L**, RISC-V, LiteOS/lwIP. Owns low-power networking, MQTT/DAS, EZLink/discovery, and the LAN wake receiver. |
| Auxiliary MCUs | Two FM33 parts: **L01** in the door unit on `/dev/ttyS2`, **L02** in the touchscreen on `/dev/ttyS1`. |
| Sensor | Loaded driver `nvt_sen_f53`; the software path is confirmed, the chip marking is not. |
| Video | Hardware HEVC. A clean live sample was **1728×1080 HEVC Main**. |
| Audio | **AAC, 16 kHz, mono** in the observed live output. |
| Display | 4.3-inch LCD (`disp_ifpa_lcd1_sat043at45dgg`). |
| Touch | Userspace I2C bus 2, slave `0x38`, plus `/dev/hikio`. Register layout resembles FT6x06; the exact controller was not identified. |
| Storage | Removable SD card at `/mnt/mmc01`. FAT32 was used for the tested upgrade and hook workflow. Recording uses preallocated 256 MiB segment rings (427 video + 47 picture), so the card reports ~100% used by design; a card "format" is a quick format, not an erase. |
| RAM | Kernel reports about 141 MB available. A 110 MiB media zone spans `0x09200000`–`0x10000000`, consistent with a 256 MiB layout; fitted DRAM was not physically identified. |
| Application flash | `app0` is a 6 MiB partition (`mtd10`). |

### Division of work

```text
Hi3861L: Wi-Fi, low-power IP/MQTT, LAN wake
    | HCC over SDIO; Linux control via netlink protocol 28
    | separate logical GPIO-2 wake assertion
    v
Novatek boot/power domain -> Linux
    ezapp: UI, settings, SD recording, local SDK, application policy
    ezdsp: capture / ISP / encode / AI hardware pipeline
    |
    +-- UART ttyS1 -> FM33 L02: battery/status telemetry
    +-- UART ttyS2 -> FM33 L01: door-unit side; PIR sensitivity/keep time
```

- The **Hi3861L** keeps the cloud MQTT connection while Linux sleeps. Its
  action-8 PIR handler is a host-requested alarm push, not the local sensor
  sampler.
- **PIR sampling and the low-power wake decision** are in the FM33 firmware.
  Human/person recognition is a separate Novatek AI stage (`ezdsp`,
  `lib_ezviz_detect_rec_nt9852x.so`, `nvt_ai`) that runs after frames exist.
- The **FM33 L01** is configured by `ezapp` with commands `0x1009`
  (sensitivity) and `0x110d` (trigger/keep time). It maps those values to PIR
  thresholds.
- The **FM33 L02** answers battery telemetry: command `4` on ttyS1 produced
  `fffe00040607` (TX) and `fffe00040a3a` + `17000216` (RX) in a live trace.
- A dock/undock test placed L01 in the door unit (`rx:0` while undocked,
  `rx:178` when docked) and L02 in the touchscreen (bidirectional in both
  states).

## Software layout

| Path | Contents |
| --- | --- |
| `/dav/ezapp` | Main application: UI, settings, recording, cloud SDK integration, local SDK. Stripped 32-bit ARM ELF, uClibc. |
| `/dav/ezdsp` | Media pipeline owner: ISF/kflow capture, ISP, HEVC encode, AI. |
| `/dav/ezupgrade` | Local upgrade helper. It performs no cryptographic check itself. |
| `/dav/da_info` | Internal diagnostic/control client. Connects to `127.0.0.1:7001` and dispatches by `argv[0]`. |
| `/dav/initrun*.sh` | Boot scripts in the app image. |
| `/bin/busybox` | Vendor BusyBox. It lacks several familiar applets (`telnetd`, `sshd`, `id`, `head`, `uname`, `passwd` in the shipped configuration). |
| `/mnt/mmc01` | Removable SD card. Local Upgrade reads `/mnt/mmc01/ezviz.dav`. |

The app partition is a LZO SquashFS image with a 128-byte `SWKH` trailer. The
root ramdisk contains the init scripts and a reduced BusyBox userland. Kernel,
bootloader, `enc`/OTP, and `/devinfo` were not modified during this research.

### Boot, sleep, and wake

- In the tested configurations, the only listeners observed were the custom
  SSH service on 2222 (after a modified app image), `ezapp` on loopback 7001,
  and UDP 50160/50161. Stock firmware exposed no LAN media service.
- Sleep tears down Linux. Wake starts it again: uptime resets and boot scripts
  rerun. The Wi-Fi MCU can remain connected while Linux is gone.
- The patched LAN receiver uses UDP **50162** and exact payload
  `43503457414b45015003bd2f`. It requests OEM Wi-Fi prewake before asserting
  logical GPIO 2. The wake tool selects it with `--protocol cp4`.
  Unmodified firmware uses the default `oem` profile. See [WAKE.md](WAKE.md).
- SD recordings are event-based `hiv*.mp4` ring files containing MPEG-PS with
  HEVC Annex-B, not a continuous live feed.

## Service surface

| Endpoint | State | Notes |
| --- | --- | --- |
| UDP 50162 | Patched MCU receiver | Fixed 12-byte packet, Wi-Fi prewake and GPIO wake. |
| UDP 50161 | Unmodified MCU receiver | OEM wake profile. |
| UDP 50160/50161 | Awake `ezapp` | Ownership depends on lifecycle. |
| TCP 2222 | Custom image only | Key-only SSH from the modified app0 image; no stock listener. |
| TCP 2323 | Historical | Raw root shell in early custom images; removed in the current design. |
| TCP 7001 (loopback) | Awake `ezapp` | `da_info` diagnostic/control service. |
| TCP 8000 | Experimental | HCNetSDK `dvrNetServer`; started in RAM by calling a setup helper. Not persisted. |
| UDP 37020 | Experimental | SADP discovery, started with the same helper. |
| TCP 554 | Closed | No working RTSP server was established. |
| Outbound TCP 8666 | Always | Cloud bootstrap/authentication. |
| Outbound TCP 8820 | While connected | Cloud-assigned MQTT/DAS broker, held by the Wi-Fi MCU. |

Normal fresh boots did **not** expose media on 554, 8000, 9010, 9020, or 7010.
Earlier references to “media on 7010” were not a local service.

## Local video research (not packaged here)

This repository does not include video tooling, but the platform findings are
useful context:

- There are no `/dev/video*` V4L2 nodes. Media uses Novatek ISF/kflow/HDAL
  devices and shared-memory rings between `ezdsp` and `ezapp`.
- The clean port-8000 route logs in as `EZ_LOCAL_USER` with a password derived
  from config record `0xf` at `+0x78`. The recovered proof is
  `HMAC-MD5(challenge, "EZ_LOCAL_USER")` for the user ID and an HMAC-MD5 over a
  SHA-256 of the local password for the proof. The listener normally has to be
  started in RAM; it is not persisted.
- A read-only tap of ring 0 of the shared stream arena produced MPEG-PS with
  HEVC 1728×1080 and AAC LC 16 kHz mono without the SDK or a login. A
  continuous reader must follow the write cursor, handle ring wrap, and
  recover parameter sets.
- The shared arena is recreated on each Linux boot, and `ezdsp` is not running
  while the device sleeps.

## What works today

| Capability | State on the tested unit |
| --- | --- |
| LAN wake | Patched UDP 50162 receiver passed initial controlled wake checks; long-term behavior and power remain unmeasured. |
| Cloud wake | Proven through the live-view preview handshake; the IoT SpeedUp action alone was not reliable. |
| Wi-Fi MCU receiver | Independent receiver with 20-second OEM prewake, cooldown and read-only MCU snapshot. Exact builder, explicit updater and temporary Linux reply patch are documented in [Hi3861L receiver](HI3861-LAN-PATCH.md). |
| Root shell | Key-only SSH on 2222 after installing a modified app0 image; SD hook as a fallback. The build recipe is in [app0 SSH](APP0-SSH.md). |
| Local upgrade | App-only modified images install and boot; no trailer-version gate was observed. |
| Firmware acquisition | Device-authenticated MQTT query returned the installed package and an HTTPS URL with MD5/size. |
| Recovery | Packages exist, but bootloader/card recovery and slot fallback were not validated. |
| Local video | Research only: SDK listener plus clean login, or a read-only shared-ring tap. No boot service. |

## Remaining gaps

- Cold startup with no prior cloud contact and long-term sleep/wake reliability.
- Behavior on other CP4 revisions, models, and firmware versions.
- The exact PIR sensor wiring and the FM33 wake line into the Novatek power
  domain.
- A validated recovery path for a boot-looping modified image.
- A reliable, boot-integrated local video service.
- Full inter-unit transport and the complete FM33 command map.
