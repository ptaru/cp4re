# Scrypted CP4 LAN camera

The [CP4 plugin](../plugins/cp4-camera/) provides on-demand LAN wake and live
HEVC/AAC video, browser viewing through WebRTC, snapshots through Snapshot,
and Apple Home integration through HomeKit. It sends the custom UDP packet
itself; Python, SSH, and EZVIZ cloud access are unnecessary at runtime.

Live viewing, snapshots, and HomeKit playback were demonstrated on the test
unit on 23–24 October 2026 with Scrypted server 0.147.0 and plugin 0.0.5.
The working HomeKit configuration used standalone **Accessory Mode** and
**protocol V3**. This is a single-device PoC, not a long-term reliability test.
Mild intermittent choppiness was observed before the final successful report.

## Prerequisites

- The [patched CP4 wake receiver](HI3861-LAN-PATCH.md), listening on UDP 50162.
- The [video server](VIDEO-SERVER.md#automatic-start-from-sd) and SD boot hook
  already working: waking Linux must start `/mnt/mmc01/cp4-video` automatically.
- The Scrypted server can reach the camera on UDP 50162 and TCP 8080.
- A Node/npm environment supported by your Scrypted installation for building.
  The camera IPv4 address is configured in settings; the plugin supports one
  camera per installation.

The camera serves unauthenticated HTTP video. Keep it on a trusted LAN.
Do not run an independent timed or forever hold alongside the camera server:
the server's guardian must exclusively own the RAM sleep-permission flag.

## Build and deploy

From the public repository root:

```sh
cd plugins/cp4-camera
npm ci
npm run check
npm test
npm run test:bundle
# Optional media regression checks; local FFmpeg with libx264 required:
npm run test:media

npx scrypted login SCRYPTED_SERVER_IP
npm run scrypted-deploy SCRYPTED_SERVER_IP
```

`test:bundle` builds `out/plugin.zip` and tests its SDK initialization. If you
edit source later, run `npm run build` before deploying again. Deployment uses
the SDK's saved login. The plugin is local development software, not published
to npm. Neither commands nor examples here contain private device identities.

## Enable viewing

1. Open **CP4 LAN Camera PoC** in Scrypted and set **Camera IPv4 address**.
   HTTP port defaults to 8080; wake timeout defaults to 60 seconds.
2. Ensure **Rebroadcast**, **Snapshot**, and **WebRTC** are installed. They are
   declared plugin dependencies; verify the appropriate camera extensions are
   enabled. Snapshot supplies frame extraction; the CP4 plugin has no separate
   snapshot cache implementation.
3. Enable **WebRTC** on the camera and refresh the console if no play control
   appears. WebRTC supplies the management console's browser playback.
4. Under **Streams → Manage**, leave **Prebuffered Streams → CP4 HEVC + AAC**
   unchecked. Save changes when needed. Open live view to wake and stream.

The VideoCamera interface exposes media, but does not provide a browser player
by itself. HEVC browser compatibility varies; use Scrypted transcoding where
required. The Snapshot extension can work even when live viewing is not set up.

## Apple Home / HomeKit

1. Install Scrypted's **HomeKit** plugin and enable its extension on the CP4.
2. Use standalone **Accessory Mode**. The tested configuration used **protocol
   V3**, which allowed playback with this HEVC source on the user's Apple setup.
3. Reload HomeKit as required after changing its settings. Open the camera's
   **HomeKit → Pairing** section and scan its own QR code in Apple Home.
4. Test live viewing, then close the viewer and check that the upstream stream
   eventually stops.

V3 availability and HEVC support depend on the installed HomeKit plugin and
Apple clients/hubs. Legacy H.264-only playback requires transcoding on the
Scrypted server. The plugin does not change the camera's hardware encoder.
See [Scrypted's HomeKit guide](https://docs.scrypted.app/homekit) for current
protocol and hub requirements.

Motion events, doorbell presses, two-way audio, discovery, and recording access
are not implemented. In particular, motion-triggered HomeKit Secure Video is
not established by this PoC. Do not enable continuous consumers if the camera
should sleep between views.

## Media path and sleep lifecycle

```text
Scrypted stream request
  → TCP readiness check; UDP wake only when needed
  → camera Linux boots; app0 starts SD hook and HTTP server
  → on-demand FFmpeg bridge on Scrypted: MPEG-PS → fragmented MP4
  → Rebroadcast: fragmented MP4 → RTSP
  → WebRTC / Snapshot / HomeKit consumer
```

The wake datagram is 12 bytes, hex `43503457414b45015003bd2f`, sent to UDP
50162. Attempts are at least five seconds apart and stop when TCP 8080 answers.
Concurrent requests with the same settings share a wake attempt. Merely listing
settings or stream options does not wake or poll the camera. TCP readiness
checks do not request video; a listening port does not guarantee media readiness.

The camera's stream is HEVC Main 1728×1080 at approximately 15 fps, with AAC
16 kHz mono. The loopback bridge uses `-c copy` and `aac_adtstoasc`, then delays
the initial MP4 header until AAC codec configuration is available. This allows
RTSP to generate valid AAC SDP (`config=1408`). No re-encoding occurs in this
bridge, although downstream WebRTC/HomeKit may transcode for a client.

MP4 fragments have a 200 ms duration limit and are flushed promptly. Fragmenting
only at keyframes caused roughly three-second playback bursts and freezes.
The limit is in `src/bridge.ts`: `'-frag_duration', '200000'`. A possible future
experiment is 100000 (100 ms), adding container/write overhead on Scrypted,
without materially changing the camera's stream. The source default remains
200 ms; smaller fragments are not a demonstrated fix for all choppiness.

Each consumed bridge HTTP connection starts an FFmpeg remux process. Closing
the downstream connection terminates that process and its camera connection.
Rebroadcast may keep the connection for an inactivity grace period. Once the
last camera viewer disconnects, the guardian releases sleep permission; this
permits sleep rather than forcing immediate sleep. Cold boot and the known
camera disruption near source PTS 30 seconds can still affect startup.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No play button, but snapshot works | Install/enable WebRTC on the camera and refresh the console. |
| `getDeviceState` on undefined during load | Update to 0.0.2 or newer and rebuild; the bundle now handles SDK runtime bindings explicitly. |
| `AAC with no global headers` | Update to 0.0.3 or newer; direct MPEG-PS → RTSP copy lacked AAC configuration. A filter on the RTSP output alone was too late for SDP creation. |
| Repeated seconds-long playback freezes | Update to 0.0.5 or newer; short timed fragments replace GOP-sized delivery bursts. |
| Periodic wake or no sleep | Disable continuous prebuffering, recording, periodic snapshot refresh, and close live viewers. Inspect remaining consumers. |
| `active rebroadcast clients: 0`, then inactivity shutdown and `stream ended` | Usually normal teardown if no viewer remains; it is not by itself a camera failure. |
| HomeKit protocol/mode change followed by no sleep | Inspect for a stale HomeKit FFmpeg process as described below. |
| `scheduled plugin restart cancelled, plugin was restarted by user` | Scrypted manager message: a manual restart/redeploy superseded a scheduled restart. |

### A leftover HomeKit consumer can hold the camera awake

One observed incident followed HomeKit reconfiguration. The camera had an
established TCP 8080 connection from Scrypted despite prebuffering being off.
The console showed one remaining rebroadcast client after WebRTC closed.

On the Scrypted server, an FFmpeg process consuming the CP4 RTSP device path had
**PPID 1**, while the current HomeKit plugin had a different PID. This supported
the conclusion that the previous parent exited and left its stream process
adopted by init. The process was still sending SRTP to an Apple client. The
specific setting change causing the orphan was not proven.

Use Scrypted's **Terminal** to inspect processes, preferably printing command
lines locally rather than posting them: HomeKit FFmpeg arguments can contain
SRTP session keys. Identify the CP4 device path, PID, PPID, and current HomeKit
parent before acting. PPID 1 alone is not sufficient to identify an unwanted
process. Stop only a confirmed leftover process with `kill -TERM PID`; avoid
broad `killall ffmpeg` commands that affect other cameras or active viewers.

In this incident, terminating that specific orphan caused Rebroadcast and the
CP4 bridge processes to exit automatically. The camera became unreachable
shortly afterwards, consistent with sleep. No sleep flag readback was performed
then, so unreachability alone is not proof of its exact power state. Normal
playback subsequently worked well. No recurring cleanup daemon was installed.

## Checks and limitations

- Wake tests cover exact packet bytes, already-awake behavior, retry spacing,
  timeout, concurrent requests, and real loopback TCP readiness checks.
- Bundle tests load the built artifact with current CJS, legacy, and global
  SDK runtime bindings.
- Media tests verify AAC configuration survives remuxing and timed fragments
  arrive with subsecond gaps despite a three-second synthetic GOP.
- Live integration was tested on one device and one Scrypted installation.
  Long-term reliability, battery consumption, cold-start timing guarantees,
  and motion-triggered recording remain unmeasured.
