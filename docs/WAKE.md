# Wake and sleep

The CP4 sleeps aggressively. While it sleeps, the Wi-Fi MCU can stay connected
to the cloud, but Linux and every TCP service are gone. Two independent wake
paths were demonstrated on the tested unit: a LAN UDP packet and a cloud
preview handshake.

## LAN wake (preferred)

```text
destination:  <camera-ip>:50161/udp
payload:      50 03 00 00 bd 0c 2f   (7 bytes)
source port:  ephemeral (any)
```

One ordinary unprivileged UDP datagram is enough. No account, cloud
connection, replacement bootstrap server, device key, encrypted body, or raw
socket is involved.

### Why it works

The Hi3861L firmware contains a UDP receiver on port 50161. Its classifier
uses a bit selector and an expected-byte array. For the recovered EZLink
fallback, selector `53` has bits 0, 1, 4, and 6 set, selecting expected bytes
`50`, `03`, `bd`, `/`; bytes 2, 3, and 5 are ignored. An accepted packet sets
wake reason 4, calls the same final helper used by the MQTT host-wake route,
and asserts **logical GPIO 2** into the Novatek power domain.

There is no cryptographic authentication, pairing lookup, or encrypted-body
parser on that matched-header-to-GPIO branch. The payload is an EZLink wake
pattern supplied by the host library, so it is **not derived from the device
serial**. It may still be model or firmware specific; `wake.py --payload`
overrides it.

### Evidence

Three controlled tests isolated the camera from WAN traffic with temporary
router rules and recorded fresh Linux uptime:

| Stimulus | Result |
| --- | --- |
| ASCII negative control to 50161, then the seven-byte header | Control produced no wake. The header opened the shell 5.63 s later; Linux uptime was 3.85 s. |
| Seven-byte header alone, router source port 50161 | Shell opened 10.87 s later; uptime 9.37 s. |
| Ordinary Python UDP socket, ephemeral source port | Shell opened 4.07 s later; uptime 3.76 s. |

The camera's `0x8d8d` EtherType discovery frames appeared **after** a
successful wake, not before it. Raw Ethernet wake was not demonstrated and is
not needed. Ports 31236/31237 appear in ARM sleep-preparation code but are not
the receiver's bind port.

## Cloud wake (fallback)

The reliable cloud trigger was the **live-view (VTM) preview handshake**. In
controlled tests the camera woke about 5 s after the request, even when the
connection was closed immediately after `StreamInfoRsp` (close-early). Keeping
the connection open also worked but was not required.

The `EnergySaving/SpeedUpListeningFrequency` IoT action (`action=2`,
`duration=60`) returned a successful API response, but the camera did **not**
wake in the first controlled action-only test. A later test that also started
a preview did wake. `wake.py` therefore defaults to `--cloud-mode preview`;
`action` and `both` are available for experiments.

Cloud wake needs an EZVIZ account that owns the camera. The tool logs in to the
regional API (`--api-url`, default EU), starts a preview, and optionally
verifies the wake through a LAN check port. No token or password is saved.

## Using the tool

```sh
# LAN first; cloud preview fallback if the LAN check does not come up.
python tools/wake.py \
  --host 192.168.1.50 \
  --serial ABC12345 \
  --account you@example.com

# LAN only.
python tools/wake.py --host 192.168.1.50 --method lan

# Wake, then wait up to 60 seconds for a custom port to open.
python tools/wake.py --host 192.168.1.50 --wait 60 --check-port 2222

# Cloud only, with no LAN address to verify against.
python tools/wake.py --serial ABC12345 --account you@example.com --method cloud
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--method auto\|lan\|cloud` | Which path(s) to try. `auto` tries LAN first, then cloud. |
| `--cloud-mode preview\|action\|both` | Cloud trigger. Default `preview`. |
| `--wait SECONDS` | How long to wait for a check port after each trigger; `0` only sends. |
| `--check-port PORT` | TCP port that means “awake”; repeatable. Default `2222` and `2323`. |
| `--lan-port 50161` | Wake receiver port. |
| `--payload HEX` | Override the seven-byte wake header. |
| `--dry-run` | Print the plan without sending anything. |

The account password is read from `EZVIZ_PASSWORD` or a hidden prompt. The
device serial and host have no defaults. The tool stores nothing.

### What “awake” means

The default readiness check is TCP **2222** (key-only SSH from a custom app0
image) and TCP **2323** (historical raw shell). Stock firmware exposes neither,
so a stock camera will look asleep even after a successful LAN wake. Choose a
port that exists on your image with `--check-port`. `tools/probe.py` shows
which candidate ports are open.

## Sleep behavior and limits

- Wake-from-sleep restarts Linux. Uptime resets and the boot scripts run again.
- A cold start with no prior cloud contact was **not** verified. An initial
  boot that has never contacted the EZVIZ cloud may behave differently.
- Long-term sleep/wake reliability, behavior after a factory reset, and other
  firmware versions were not tested.
- The demonstrated wake does not establish a local video protocol. It only
  brings Linux up.
- A sleeping camera may still have an active Wi-Fi MCU connection to the
  cloud. “No TCP services” is not proof that the device is powered off.

## Repeated-wake limit

Unmodified Hi3861L firmware counts packets matching the LAN wake pattern,
including packets received while Linux is awake. The decoded routine refuses
packet 100 onward in a fresh run-time bucket; a previous-bucket count of 200
or more can also block acceptance. Buckets use MCU run time in 86,400-second
intervals. Time advancement during deep sleep has not been measured.

Send one packet per wake attempt, then poll the readiness port without sending
more packets. Draining buffered packets, rearming the pattern and rebooting
Linux do not clear these counters. A true MCU reset can clear the state.
Association loss, failed pattern arming and listener/buffer failures are other
possible causes of lost wake.

The [Hi3861L LAN-wake patch](HI3861-LAN-PATCH.md) removes this counter-based
refusal. Boot, sleep and LAN wake worked after installation, including a wake
after 110 paced matching datagrams. MCU code/counters were not read back and
long-term reliability remains untested. The patch requires a separate MCU
update; its binary and launcher are not included here.

## Troubleshooting

| Symptom | Things to check |
| --- | --- |
| LAN wake sends but nothing opens | Wrong camera IP or VLAN, client isolation, a firewall dropping UDP 50161, or a different model/firmware whose wake pattern differs. Try `--payload` from a capture. |
| Wake worked earlier, then stopped | The Hi3861L repeated-wake defense may have tripped. Pace attempts and try a true power cycle. See “Repeated-wake limit” above. |
| The camera was awake but no check port opened | Stock firmware has no 2222/2323. Use `tools/probe.py` and a port that exists. |
| Cloud login fails | Wrong region (`--api-url`), MFA/SMS requirement, or account password. The client automatically retries a region redirect once. |
| Cloud trigger succeeds but no wake | Confirm the serial belongs to the account. Try `--cloud-mode both`; the close-early preview handshake is the proven path. |
| Wake worked but SSH times out | Linux may still be booting; increase `--wait`, or the image may not start SSH. |
