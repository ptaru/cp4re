# CP4 standalone video server

Development home for the static ARM shared-ring HTTP server and deployer.
See [the video server guide](../../docs/VIDEO-SERVER.md) for operation,
architecture, validation, and known limitations.

From the public repository root:

```sh
ZIG=/path/to/zig sh tools/video_server/build.sh
python3 tools/video_server/deploy.py --host 192.168.1.50 --protocol cp4
ffplay http://192.168.1.50:8080/live.ps
```

Use `--protocol oem` for the unmodified wake receiver (the default).
Use `--dry-run` to inspect the wake profile without contacting the camera.
The ARM executable is generated locally and is not tracked by Git.
