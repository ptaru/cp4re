# Hi3861L LAN-wake patch

The CP4's Hi3861L firmware limits repeated LAN wake requests. A two-byte patch
removes the receiver's counter-based refusal while preserving packet matching,
sleep arming and normal GPIO wake handling.

The addresses and image hashes below apply to the CP4 Hi3861L application
version **2.139**. The patched image was installed on a CP4 and supports normal
boot, Wi-Fi, SSH and LAN wake. The firmware builder, patched binary and flash
launcher are not included in this repository.

## Wake counter behavior

During sleep preparation, Linux `ezapp` sends the EZLink pattern to the MCU.
The MCU's UDP 50161 receiver matches incoming packets against that pattern,
then calls the defense/statistics routine at `0x420a82` before asserting GPIO 2.

The decoded routine uses 86,400-second MCU run-time buckets:

- A fresh bucket accepts matched packets 1–99 and refuses packet 100 onward.
- A previous-bucket count of 200 or more can also block acceptance.
- A gap of more than two buckets resets the state but refuses the first packet.
- Matched packets count even while Linux is awake.

These thresholds come from instruction-level analysis. MCU time advancement
during deep sleep has not been measured. The buckets are not UTC calendar days.

Draining buffered packets and rearming the wake pattern do not clear the
counter globals. An ordinary Linux reboot also leaves the MCU running. A true
MCU reset can clear the state; no supported host command to reset these
counters has been identified.

For unmodified firmware, send one packet per wake attempt and wait for Linux
to boot before retrying. Keep SSH-readiness polling separate from packet
sending. See [Wake and sleep](WAKE.md) for the packet and sender commands.

## Code change

Replace the conditional branch after the defense call with an unconditional
jump to the same accepted-packet path:

| Item | Value |
| --- | --- |
| Runtime address | `0x420dc4` |
| Expanded image file offset | `0x13dc4` |
| Original bytes | `09 e5` — `c.bnez a0, +10` |
| Replacement bytes | `29 a0` — `c.j +10` |
| Branch target | `0x420dce` |

The call at `0x420dc0` still runs and updates its counters and logs. Its return
value no longer blocks the receiver. This applies to every packet reaching
that branch, including packets other than the seven-byte EZLink wake header.

Packet matching, wake reason 4, the GPIO-2 helper, duplicate detection, packet
buffering, sleep rearm and the `req/8233` prewake classifier remain unchanged.
Association loss, failed host arming, listener exits and full packet buffers
can still prevent wake. The patch adds neither cold-start arming nor packet
authentication. Continue pacing requests to avoid unnecessary host wakes and
battery use.

## Image packaging

The source is `Hi3861L_ezviz_sdk_ota.bin` from app0. It is a compressed MCU
application OTA, separate from the camera `.dav` package and the flashboot OTA.

| Item | Value |
| --- | --- |
| Source SHA-256 | `e01c4867892f611326c4935abdcfb1d9a6c19629f2953b0cd441fb36578596c2` |
| Patched OTA SHA-256 | `5eb5d27c9ac86fc0d9b7a6dc627a5ec1b910e1ccd65290dd7e8e5d0f29f9e734` |
| OTA size | 552,528 bytes, unchanged |
| Expanded image size | 911,792 bytes |
| Expanded differences | Two instruction bytes and the 32-byte inner section hash |
| Vendor version | 2 / 139, unchanged |
| Encoder | LZMA SDK 19.00 |
| LZMA settings | `lc=2, lp=2, pb=2, dict=65536`, 128 fast bytes, normal algorithm, BT4 |
| Stream framing | Known expanded size in the 13-byte header; no end marker |

To rebuild the image:

1. Check the source hash and original instruction bytes.
2. Decompress the OTA and apply the two-byte instruction change.
3. Regenerate the expanded image's section SHA-256.
4. Recompress using the settings above. First verify that the encoder reproduces
   the untouched source stream byte-for-byte.
5. Preserve the common wrapper and total OTA size, adjust payload padding, and
   regenerate the outer header and section hashes.
6. Verify all hashes, decompress the result and compare it with the expected
   expanded image. Only the instruction and inner section hash should differ.

The patched stream is six bytes larger than the source stream; trailing padding
shrinks from ten bytes to four. The packaged image also passes an independent
LZMA SDK 9.20 streaming decoder with input chunks of 1, 13, 64 and 4,096 bytes.

Application and flashboot verification both select between SHA-256 comparison
and RSA/ECC verification using eFuse configuration. The source packages contain
plain SHA-256 hashes. Recalculating these hashes applies to the hash-only path;
it does not satisfy RSA/ECC verification. The live eFuse byte has not been read.
The patch changes neither eFuse nor the bootloader.

## Installation path

Replacing the bundled OTA in app0 is insufficient for this same-version image:
`ezapp` skips installation when its cached MCU version matches the file version.
The version fields at file offsets `+0x18` and `+0x1a` also select the update:

| Version comparison | Action |
| --- | --- |
| Same major and minor | Skip update |
| Same major, different minor | Update application kernel |
| Different major | Update flashboot |

Keep version 2.139 for this patch and explicitly invoke the kernel updater.
Changing the major version can select a bootloader update.

The installation used `ezapp:0x2fd9c(path, 10)` on a fresh pthread inside the
running `ezapp`, through its existing HCC connection. The image was opened
read-only and pinned through `/proc/HELPER_PID/fd/N`. Short setup and cleanup
calls borrowed the idle main thread with register and temporary-stack restoration.
Starting another library instance that competes for the HCC/netlink owner is
not an established installation route.

The OEM wrapper handles transfer retries, verification, a ten-second delay and
host reboot. Keep continuous external power during an update. If networking
drops, inspect the update log and boot state before issuing another update;
connection loss alone does not indicate success.

Software restoration requires the MCU to boot far enough to service HCC. No
UART/JTAG recovery procedure is available for this setup, so an unbootable MCU
may be unrecoverable. Reinstalling app0 does not by itself restore the MCU.

## Test results

Installation on 23 October 2026 was followed by a fresh Linux boot with working
Wi-Fi and SSH. Normal sleep and single-packet LAN wake also worked.

During a 60-second awake hold, 110 matching datagrams were sent at 0.1-second
intervals. After the hold expired and SSH became unreachable, one LAN packet
restored SSH with uptime 3.34 seconds. This supports operation beyond the
original current-bucket allowance. Individual packet receipt and counter
increments were not measured, and this test was not WAN-isolated.

The final updater return and MCU binary readback were not captured. Long-term
sleep/wake reliability, AP reassociation, cold startup and power-loss recovery
remain untested.

## Related

- [Wake and sleep](WAKE.md) — packet format, sender commands and troubleshooting.
- [Platform guide](PLATFORM.md) — Wi-Fi MCU and host architecture.
- [Firmware](FIRMWARE.md) — camera packages and app0 installation.
