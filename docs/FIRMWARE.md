# Firmware: container, acquisition, and rebuilding

This document covers the `.dav` package format, how the cloud package was
obtained, and how to build a modified app image. It is an offline reference
plus the behavior of `tools/dav.py` and `tools/make_firmware.py`.

> **Read the safety notes first.** Firmware changes can make the device
> unreachable. Recovery was not validated on the tested unit. Keep the
> untouched official package and a recovery image on separate SD cards.

## The `.dav` container

The CP4 package is an `SWKH` container. The tested V5.3.3 package has a
460-byte header and nine components:

| Component | Role |
| --- | --- |
| `app.img` | Application SquashFS (Linux userland under `/dav`). |
| `uImage` | Linux kernel. |
| `mImage` | Secondary/boot image. |
| `fdt.app.bin` | Application device tree. |
| `fdt.bin`, `fdt.hdr.bin` | Device-tree variants. |
| `ramdisk.bin` | Root ramdisk. |
| `rtos.bin` | RTOS boot/power image. |
| `ai.bin` | AI model/firmware. |

### Header layout

The first 460 bytes are XOR-obfuscated with a fixed 16-byte key:

```text
BACDBCFED6CADDD3BAB9A3ABBFCBB5BE
```

The XOR index is `(i + (i >> 4)) & 15`. Decoding is symmetric.

Decoded header:

| Offset | Size | Field |
| ---: | ---: | --- |
| `0x00` | 4 | Magic `SWKH` |
| `0x04` | 4 | Header checksum: additive sum of decoded bytes `[0x0c:header_length]` |
| `0x08` | 4 | Header length (460 for the tested package) |
| `0x0c` | 4 | Entry count (9) |
| `0x24` | 1 | `secure_flag` (`0xff` on the official CP4 package; security firmware expects `0x87`) |
| `0x25` | 1 | `firm_sec`; encrypted-firmware mode is `2` (not set here) |
| `0x2c` | 20 | Platform string |
| `0x40` | ... | Entry table, 44 bytes per entry |

Each entry:

| Offset | Size | Field |
| ---: | ---: | --- |
| `0x00` | 32 | Component name, NUL-padded |
| `0x20` | 4 | Payload offset |
| `0x24` | 4 | Payload size |
| `0x28` | 4 | Additive byte sum of the payload |

Components are plaintext and packed back to back after the header. All
checksums are 32-bit **additive byte sums**. They detect corruption; they are
not a cryptographic signature.

`tools/dav.py verify --roundtrip` validates the header checksum, every
component checksum, the trailing region, and that the decoded header re-encodes
byte-for-byte.

### `app.img`

`app.img` is a **SquashFS 4.0** image, LZO compression, 131072-byte blocks. It
is padded with zeros to a 4096-byte boundary, then followed by a **128-byte
`SWKH` trailer**:

| Offset | Field |
| ---: | --- |
| `0x00` | `SWKH` |
| `0x04` | Additive sum of the image bytes before the trailer |
| `0x08` | Tag (`y50` in the tested package) |
| `0x18` | Version string, e.g. `5.3.3` (maximum 8 bytes) |
| `0x58` | `0xff` padding; no signature material |

The trailer version is **identification only**. On the tested unit, Local
Upgrade accepted a 5.3.3-trailer image containing 5.3.16 content and booted it
normally: no trailer-version gate was observed. Settings still displayed
5.3.3 because the UI calls a version formatter compiled into the unchanged
`ezapp`.

The app0 partition is **6 MiB** (`0x600000`). The image plus trailer must fit.
The tested TinySSH image had only about 4 KB free.

## Acquiring firmware

### What you need

- The device **serial**.
- The device **verification code** (shown on the CP4 touchscreen).
- The device **identity** (32 bytes), sent in the clear during the device's
  own legacy bootstrap exchange. Capture it with `tools/device_id.py`.
- The device **model** (for the query), e.g. `CS-CP4-R100-6E2WPFBS`.
- A firmware version to advertise. The cloud returns a package only when the
  advertised version is older than the latest one it knows. The tool tries the
  account/current version first, then an intentionally old
  `V0.0.0 build 000000` fallback.

### Device identity capture

The identity appears in the legacy channel-99 handshake on TCP 8666, in
AUTH-III (command 3) and refresh (command 7). Capture while the camera
reconnects or boots:

```sh
sudo tcpdump -i br-lan -w cam-boot.pcap 'host 192.168.1.50 and tcp port 8666'
# power-cycle or re-dock the camera
python tools/device_id.py cam-boot.pcap --write device-id.json
```

The identity is a stable identifier, not a password, but treat it as private.
The capture also contains encrypted session material.

pcapng is not supported. Convert it first:

```sh
editcap -F pcap cam-boot.pcapng cam-boot.pcap
```

### Authentication and query

`tools/get_firmware.py` performs the proven route:

1. Connect to the regional LBS/bootstrap host on TCP 8666.
2. AUTH-I/AUTH-II with the serial and verification code (legacy auth mode 0).
3. AUTH-III/AUTH-IV using the existing 32-byte identity.
4. Request DAS redirection (`Mode=0`).
5. Connect to the returned MQTT broker as the device.
6. Publish an encrypted firmware metadata request to `/1100/10339`.
7. Read the encrypted response on `/serial/1100/10340`.
8. Download the `FileInfo` path and verify its MD5 and size.

The request body:

```xml
<Request>
  <DevSerial>ABC12345</DevSerial>
  <Model>CS-CP4-R100-6E2WPFBS</Model>
  <Authorization />
  <VersionInfo CurFw="V0.0.0 build 000000" ReqFw="0"
               FirmwareIdentificationCode="" Serial="ABC12345" />
  <Channel>0</Channel>
  <OtaType>0</OtaType>
</Request>
```

The response:

```xml
<Response>
  <Result>0</Result>
  <UpgradeSvrHttps Address="devupgrade.example" Port="443" ... />
  <FileInfo Name="/<md5>/digicap.dav" CheckSum="<md5>"
            Version="V5.3.3 build 251118" Size="21578316" ... />
  <Interval>15</Interval>
</Response>
```

`Result=0` with a non-empty `FileInfo Name` means a package is available.
`Result=1049939` means “no applicable update” for the advertised version. The
tool tries the account/current version first (if known) and then the fallback
version, with `FirmwareIdentificationCode` empty and `0`.

> **Disruption warning.** Existing-device authentication rotates the device's
> master/session keys. The physical camera may reconnect or reauthenticate,
> and its cloud session can be interrupted. Use `--state` to refresh the saved
> keys on later runs instead of authenticating again. Do not run this while
> you depend on the camera's cloud connection.

### `--register`

If you do not have the device identity, `--register` asks the cloud to
allocate a new one through the device-creation flow. This is more disruptive:
it can invalidate or rotate the physical device's credentials. This code path
was implemented from the protocol but was **not** exercised on the tested
device; the recommended path is a one-time capture plus `--state`.

## Rebuilding an app image

The tested build sequence:

```sh
# 1. Extract the container and the app filesystem.
python tools/dav.py unpack official.dav unpacked/
python tools/dav.py unpack-app unpacked/app.img app-root/

# 2. Modify app-root/ (init scripts, binaries, keys, etc.).

# 3. Build the image, refresh the trailer, replace app.img, verify.
python tools/make_firmware.py \
  --base official.dav \
  --app-root app-root \
  --version 5.3.16 \
  --out custom.dav
```

`make_firmware.py` runs the equivalent of:

```sh
mksquashfs app-root app.squashfs \
  -comp lzo -b 131072 -noappend -all-root -no-xattrs -no-progress

python tools/dav.py trailer unpacked/app.img app.squashfs app.img --version 5.3.16
python tools/dav.py repack official.dav custom.dav --replace app.img=app.img
python tools/dav.py verify custom.dav --roundtrip
```

The tool keeps the original 64-byte header prefix (secure flags, platform
string, etc.), rebuilds the entry table, recomputes every additive checksum,
pads the SquashFS to 4096 bytes, refreshes the trailer image sum, and enforces
the 6 MiB partition limit. It refuses to write an image that does not verify.

For a worked app change, see [app0 SSH](APP0-SSH.md): it adds TinySSH
(key-only, TCP 2222), its host/authorized keys, `inetd.conf`, and the
`initrun.sh` block, and documents the non-multiuser patch the camera kernel
requires.

### Recovery image

A recovery package keeps the base's original app contents and only raises the
trailer version:

```sh
python tools/make_firmware.py \
  --base official.dav --recovery --version 5.3.17 --out recovery.dav
```

The trailer version is identification only, so this does not perform any
downgrade or restore hidden state. It is simply the untouched app image in a
higher-versioned container. Keep the official package as the real fallback.

### Building a recovery or diagnostic image by hand

```sh
# Split the original app.img: bytes before the 128-byte trailer.
python -c "from pathlib import Path; d=Path('unpacked/app.img').read_bytes(); Path('app.squashfs').write_bytes(d[:-128])"

# Re-append a trailer with a new version.
python tools/dav.py trailer unpacked/app.img app.squashfs app.img --version 5.3.17
python tools/dav.py repack official.dav recovery.dav --replace app.img=app.img
```

## Installing

Local Upgrade reads **`/mnt/mmc01/ezviz.dav`** from the SD card:

1. Use a FAT32 card.
2. Copy the built package to the card root as `ezviz.dav`.
3. Insert the card and use the touchscreen **Local Upgrade** flow.
4. Keep power applied. Do not remove the card during the write.
5. The UI may freeze after the upgrade instead of rebooting. When the upgrade
   reports completion, power-cycle the camera.
6. Reconnect after boot and verify the image is active.

On the tested unit a modified app-only image installed and booted with no
version gate. Only `app.img` was replaced; kernel, bootloader, OTP/`enc`, and
`/devinfo` were untouched.

## Recovery and hard limits

- Keep the official package and a recovery package on **separate** SD cards.
- If the camera boots far enough to show the UI, use Local Upgrade with the
  recovery package or the official package.
- If it boot-loops before the UI, the NOR bootloader has TF auto-upgrade
  support in its scripts, but whether it accepts the card without a recovery
  trigger was **not validated**.
- The device has `app0`/`app1` slots and a `boot_from` selection. Bootloader
  fallback to `PART_BKUP` is the intended safety net, but it was not
  validated from the package alone.
- The last resort is the UART console on `ttyS0` at 115200 (U-Boot). That
  requires opening the device.

**Do not:**

- Write `cipher_otp.bin` or any OTP region. OTP writes are irreversible.
- Modify `app1`, `uboot`, `enc`, `/devinfo`, or the kernel unless you have a
  validated recovery path.
- Assume another CP4 revision or firmware has the same secure-boot state or
  update policy.

## Secure boot and signing observations

- The official CP4 package has `secure_flag=0xff`, not the security-firmware
  value `0x87`; it has no signature field and no trailing signature.
- `ezapp` contains an RSA-2048 PKCS#1/SHA-256 firmware check and a public key,
  but its gate is zeroed at init and never written in the inspected image, so
  the branch is dormant.
- The local SD upgrade task passes no expected MD5; it checks container length
  and per-component additive sums only. `ezupgrade` performs no cryptographic
  check itself.
- `libeztools.so` contains the real secure-boot stack (`SCB_STATUS=SCB_EN`,
  `secure_flag` enforcement, AES-CBC + RSA secure partitions, OTP unwrapping).
  It activates only for `secure_flag=0x87` packages. On the tested unit
  `SCB_STATUS` was not defined in the U-Boot environment, so the gate was off.

These are observations about one unit and one package. They are not a claim
that secure boot is absent on every CP4 or every firmware revision.
