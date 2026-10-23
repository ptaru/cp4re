# CP4 LAN Camera PoC for Scrypted

On-demand video for the patched EZVIZ CP4: direct UDP wake, HEVC/AAC ingest,
WebRTC viewing, snapshots, and HomeKit integration. No Python, SSH, or cloud
credentials are needed at runtime.

See the [Scrypted setup and troubleshooting guide](../../docs/SCRYPTED.md) for
camera prerequisites, deployment, enabling the viewer, HomeKit Accessory Mode
and protocol V3, sleep behavior, validation, and known limitations.

From this directory:

```sh
npm ci
npm run check
npm test
npm run test:bundle
npx scrypted login SCRYPTED_SERVER_IP
npm run scrypted-deploy SCRYPTED_SERVER_IP
```

For source edits, rebuild with `npm run build` before redeploying.
Optional `npm run test:media` requires local FFmpeg with libx264.

| Source | Purpose |
| --- | --- |
| `src/main.ts` | Camera settings and Scrypted media interface. |
| `src/wake.ts` | Direct patched-protocol UDP wake and TCP readiness. |
| `src/bridge.ts` | On-demand loopback MP4 remux, AAC headers, and timed fragments. |
| `webpack.nodejs.config.js` | SDK runtime binding compatibility for the bundle. |

Current source is 0.0.5, with a 200 ms fragment limit. Disable continuous
prebuffering to allow sleep. Browser playback needs the WebRTC extension.
The plugin is not published to npm and supports one camera per installation.
