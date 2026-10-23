# Legacy channel-99 protocol

This is the device-side protocol used by `tools/get_firmware.py`. It is the
legacy MD5/SHA-384 scheme recovered from CP4/DP2C firmware and used by the
tested camera's device bootstrap on TCP 8666. The physical camera also has a
newer ECDH-style full-auth exchange for normal operation; this document and
the tool do **not** implement that newer exchange.

The protocol family is corroborated by a Hikvision-coauthored IETF draft,
*draft-wang-open-service-access-protocol* (latest revision expired; not an
adopted standard). The draft describes LBS authentication, DAS redirection,
AES-128 encrypted MQTT bodies, verification-code-plus-serial shared-key
derivation with `www.88075998.com`, and session-key refresh. Do not substitute
the draft's newer PBKDF2/ECDH description for the legacy MD5/SHA-384
implementation used here.

> Use this only on a device you own. The handshake authenticates as the device
> and rotates its master/session keys.

## Framing

TCP 8666 carries frames:

```text
first byte:  command (high nibble) | flags (low nibble, 0)
next 1..4 bytes: remaining length, 7 bits per byte, least-significant first
body:        exactly that many bytes
```

Commands used by the legacy device flow:

| Command | Direction | Meaning |
| ---: | --- | --- |
| 1 | client -> server | AUTH-I |
| 2 | server -> client | AUTH-II challenge |
| 3 | client -> server | AUTH-III existing identity |
| 4 | client -> server | AUTH-III create new identity |
| 5 | server -> client | AUTH-IV for existing identity |
| 6 | server -> client | AUTH-IV for a created identity |
| 7 | client -> server | Refresh-I (cached master) |
| 8 | server -> client | Refresh-II challenge/session |
| 9 | client -> server | Refresh-III proof (no response) |
| 10 | client -> server | DAS request |
| 11 | server -> client | DAS response |

All multi-byte integers in the crypto inputs are big-endian where the wire
format requires it; the frame length uses the 7-bit continuation above.

## Shared key

The shared key is derived from the device verification code and the serial:

```text
first  = uppercase_hex(MD5(verification_code + serial))
second = uppercase_hex(MD5(first + "www.88075998.com"))
shared = uppercase_hex(MD5(second))
```

`shared` is 32 ASCII characters (used as the HMAC key material). The
verification code is the code shown on the device, not the account password.

## AUTH-I (command 1)

```text
01 02 00        protocol 1.2.0
<mode>          0 for the serial + verification-code mode used here
<len>           serial length
<serial>
<n1>            one random byte
<sig>           32-byte HMAC-SHA256(shared, SHA256(serial + n1))
```

The tested camera accepted auth mode **0**. Mode 1 was the wrong identity mode
for this device and returned a verification-code lookup failure. Status `5` /
SDK error `10005` in AUTH-II means the platform could not find a verification
code for that serial/mode; it does not by itself prove the code is wrong.

## AUTH-II (command 2)

A successful body is 37 bytes:

```text
01 00 00        protocol 1.0.0 (or 01 03 00)
00              status
<n2>            one random byte
<sig>           32-byte HMAC-SHA256(shared, SHA256(serial + n1 + n2))
```

A nonzero status byte is a rejection. Verify the signature before continuing.

## AUTH-III existing identity (command 3)

The device identity is 32 bytes. It is sent in the clear:

```text
01 01 00
<n3>            one random byte
0x20            identity length
<device_id>     32 bytes
<sig>           32-byte HMAC-SHA256(shared, SHA256(serial + n2 + n3))
```

## AUTH-IV existing identity (command 5)

A successful body is 70 bytes:

```text
01 01 00
00              status
<n4>            one random byte
32              encrypted session-key length
<enc_session>   32 bytes, AES-CBC encrypted
<sig>           32-byte HMAC-SHA256(shared, SHA256(serial + n3 + n4))
```

The master key is derived from the four nonces:

```text
master = uppercase_hex(SHA384(n1 + n2 + n3 + n4)[:8])   # 16 ASCII bytes
session = AES-CBC-decrypt(master, enc_session)          # 16 raw bytes
```

AES-CBC uses PKCS#7 padding and the fixed IV:

```text
"01234567" + 00 00 00 00 00 00 00 00
```

## Creating a new identity (commands 4/6)

If the device identity is not known, AUTH-III command 4 asks the server to
allocate one:

```text
01 01 00
<n3>
<sig>           HMAC(shared, serial + n2 + n3)
```

The command 6 response carries the encrypted device identity, the master key,
and the session key. `get_firmware.py --register` uses this path. It is more
disruptive than reusing an existing identity because it asks the cloud to
allocate or rotate device credentials. The create path was implemented from
the protocol but was not exercised on the tested device.

## Cached-session refresh (commands 7/8/9)

With a saved master key and device identity:

```text
command 7:  01 01 00, serial length, serial, 0x20, device_id,
            AES-CBC(master, n1)
command 8:  AES-CBC(master, n1, n2, session[16])
command 9:  AES-CBC(master, n2)  (sent, no response)
```

A status of `10` in the refresh response means the platform does not know the
cached master. The device firmware then preserves its identity and performs a
fresh authentication. `get_firmware.py` falls back to full authentication when
refresh fails.

## DAS redirection (commands 10/11)

The DAS request body is:

```text
01 01 00
AES-CBC(session, JSON)
```

where the JSON is:

```json
{"DevSerial":"ABC12345","Type":"DAS","Mode":0}
```

The command 11 response decrypts to a JSON object containing `Type: "DAS"`
and a `DasInfo` dictionary with at least:

```json
{"Address":"broker.example","Port":18883,"Domain":"...","ServerID":"...","UdpPort":0}
```

`Address`/`Port` are the MQTT broker for this device. They are session
configuration from the cloud, not something the tool invents.

## MQTT

The proven device connection uses MQTT 3.1.1:

| Field | Value |
| --- | --- |
| Client ID | the 32-byte device identity (often 32 printable base64 characters) |
| Username | the device serial |
| Password | `test` (an OEM literal, not an account or device secret) |
| Clean session | yes |
| Keepalive | 30 s |
| Will topic | `/Basic/pu2cenplt/<serial>/devinfo`, NUL-padded to 128 bytes |
| Will payload | AES-CBC(session, device-info JSON) |
| Subscription | `/<serial>/#`, QoS 1 |

The will is only published on an abnormal disconnect. `get_firmware.py`
includes it by default to match the observed device profile; `--no-will`
omits it if a broker rejects the CONNECT.

Application messages use:

```text
2-byte big-endian metadata length
metadata JSON   {"CmdVer":"v1.0.0","Seq":32001,"MsgType":0}
body
```

The whole plaintext is PKCS#7 padded and AES-CBC encrypted with the session
key and the fixed IV above. Responses carry their own metadata; `Seq` echoes
the request sequence in the observed firmware responses.

## Firmware query

| Item | Value |
| --- | --- |
| Publish topic | `/1100/10339` |
| Response topic | `/<serial>/1100/10340` |
| QoS | 1 |

The XML body advertises a current firmware version:

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

The cloud returns a package only when the advertised version is older than the
latest version it knows. Reporting the actual current version produced
`Result=1049939` with no package; advertising an older build returned
`Result=0` with:

```xml
<Response>
  <Result>0</Result>
  <UpgradeSvr Address="..." Port="21" UserName="..." Password="..." MaxTryNum="0"/>
  <UpgradeSvrHttp Address="..." Port="80" UserName="" Password="" MaxTryNum="6"/>
  <UpgradeSvrHttps Address="..." Port="443" UserName="" Password="" MaxTryNum="6"/>
  <FileInfo Name="/<md5>/digicap.dav" CheckSum="<md5>"
            Version="V5.3.3 build 251118" Size="21578316" Describe="..."/>
  <Interval>15</Interval>
</Response>
```

`FileInfo CheckSum` is the package MD5. The tool downloads from
`UpgradeSvrHttps` (preferred) or `UpgradeSvrHttp` and verifies both the MD5 and
the size. The FTP server fields are vendor infrastructure and are not used.

Command `13446` (`/1100/13446`, response `/serial/1100/13447`) also returned
an empty package name in the tested case and is not used by the tool.

## Device identity format

The identity is exactly 32 bytes on the wire. On the tested device it was the
32-character ASCII base64 form, e.g. a string ending in `=`. The tool accepts:

- 32 ASCII characters (the observed form), or
- 64 hex digits for a 32-byte binary identity.

`tools/device_id.py` prints both forms.

## Statuses and troubleshooting

| Symptom | Interpretation |
| --- | --- |
| AUTH-II status 5 | Verification-code lookup failed (wrong mode, serial, or code; region mismatch). |
| AUTH-II signature mismatch | Wrong verification code or wrong serial. |
| AUTH-IV status nonzero | Wrong/unknown device identity, or the cloud rejected the existing identity. |
| Refresh status 10 | Cached master key is unknown; perform full authentication. |
| MQTT CONNACK 4/5 | Bad client identity/credentials for that broker; check the device identity and serial. |
| `Result=1049939` | No update for the advertised version. Advertise an older version. |
| No response | Wrong broker, subscription rejected, or the cloud did not process the query. Increase `--listen`. |

## Related documents

- [Firmware](FIRMWARE.md) — container format, acquisition workflow, and rebuild procedure.
- [Wake and sleep](WAKE.md) — the local UDP wake and the cloud preview wake.
- The IETF draft: `draft-wang-open-service-access-protocol` (expired; use the
  legacy MD5/SHA-384 behavior described above, not its newer ECDH description).
