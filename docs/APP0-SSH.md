# Key-only SSH in app0 (TinySSH)

The tested CP4 image provides key-only SSH on TCP **2222** from the app image,
started by `initrun.sh` **before** the SD-card wait. It has no password
authentication and no raw 2323 shell. The SD hook is an optional fallback, not
a prerequisite.

This page documents how that image was built. It does not include a prebuilt
binary, a host key, or an authorized key. Generate those yourself.

> **Security.** SSH on 2222 is a root service. Keep it on a trusted LAN,
> restrict it at the router, disable UPnP, and never forward it to the
> internet. An app image that contains a host private key is itself a root
> credential; keep it private.

## Why TinySSH needs a patch

The CP4 kernel is built without `CONFIG_MULTIUSER`. `getgroups`,
`setgroups`, `setgid`, and `setuid` return `ENOSYS`. TinySSH calls
`dropuidgid()` during authentication and channel setup; the unpatched binary
fails with errors such as `Failed to set euid` or `Error changing user group`.

Only the root account logs in, and the target uid/gid are already current, so
no privilege change is needed. The patch makes `dropuidgid()` return success
early:

```diff
--- a/dropuidgid.c
+++ b/dropuidgid.c
@@ -18,6 +18,11 @@
 */
 int dropuidgid(const char *name, uid_t uid, gid_t gid) {
 
+    /* CP4: kernel built without CONFIG_MULTIUSER. setgid/setuid/setgroups
+       return ENOSYS. The only login account is root, already uid/gid 0,
+       so no privilege change is required. */
+    if (geteuid() == uid && getegid() == gid) return 1;
+
     /* we can change group only as the root */
     if (geteuid() == 0) {
         if (setgid(gid) == -1) return 0;
```

The patch is also in
[`examples/app0/tinyssh-cp4-nonmultiuser.patch`](../examples/app0/tinyssh-cp4-nonmultiuser.patch).
It covers both call sites (the authentication subprocess and channel setup)
and skips `initgroups`/`setgroups`.

## Build record

The tested binary was built from TinySSH source, not a packaged release:

| Item | Value |
| --- | --- |
| Source | `janmojzis/tinyssh` master snapshot |
| Commit | `a75c483758d38f3511e867c063a67ae481ca38ff` |
| Source archive SHA-256 | `a3ba58c871927cf75fc18c21a792c5bcc8ffb4101136fa02ef8f412d69a9cf19` |
| Toolchain | Zig 0.17.0 (`zig-aarch64-macos-0.17.0`), `zig cc` wrapper |
| Target | `arm-linux-musleabihf`, `-mcpu=cortex_a9 -mfloat-abi=hard` |
| Output | static, non-PIE, stripped, 32-bit ARM EABI5 hard-float |
| Tested size | 170,436 bytes for `tinysshd` |

The master snapshot was used because it contains post-release PTY/security
fixes. The build used:

```sh
make tinysshd CC="$PWD/zig-cc-arm.sh" \
  CFLAGS="-W -Wall -Oz -fPIC -fwrapv -Icryptoint -static -fno-pie \
          -fno-unwind-tables -fno-asynchronous-unwind-tables \
          -fno-stack-protector -ffunction-sections -fdata-sections" \
  LDFLAGS="-static -no-pie -s -Wl,--gc-sections"
```

where the wrapper is:

```sh
#!/bin/sh
exec zig cc -target arm-linux-musleabihf -mcpu=cortex_a9 -mfloat-abi=hard "$@"
```

The feature tests were resolved against the target musl headers. The tested
build defined `HASLIMITS HASOPENPTY HASLOGINTTY HASMLOCK HASUTMP HASUTMPX`
and left `HASCLOSEFROM HASLIB25519 HASLIB1305 HASLIBNTRUPRIME
HASLIBRANDOMBYTES HASVALGRIND` undefined. `tryfeature.sh` does not run cross
binaries, so verify your own build rather than assuming these results.

`tinysshd-makekey` is the same multicall binary under a different basename:

```sh
cp tinysshd tinysshd-makekey
```

TinySSH is a separate upstream project; check its repository for its current
license terms. The patch and example files in this repository are MIT-licensed
like the rest of the project.

## Layout inside app0

```text
/dav/tinysshd                         static ARM TinySSH (multicall)
/dav/tinyssh/sshkeydir/ed25519.pk     Ed25519 host public key
/dav/tinyssh/sshkeydir/.ed25519.sk    Ed25519 host private key
/dav/tinyssh/authorized_keys          your Ed25519 public key
/dav/inetd.conf                       2222 service definition
```

Generate the host key yourself:

```sh
./tinysshd-makekey -q /path/to/sshkeydir
```

Put your **public** key in `authorized_keys`. Never publish the host private
key or a finished app image that contains it.

The tested image baked the public key and host key into app0 so SSH works with
the SD card removed. If you distribute an image, generate a fresh host key per
device and never include another person's authorized key.

## `inetd.conf`

```text
# Owner-authorized SSH on 2222 (TinySSH from app0).
2222 stream tcp nowait root /dav/tinysshd tinysshd -q /dav/tinyssh/sshkeydir
```

The argv[0] value `tinysshd` selects the multicall applet. The `-q` flag is
required with BusyBox inetd: inetd attaches the child's stderr to the socket,
so TinySSH's `-v` info log is written into the SSH stream and corrupts it
(`Bad packet length … Connection corrupted`).

## `initrun.sh` additions

Insert this block after `/dav` is available and **before** the SD-card wait.
The full example is
[`examples/app0/initrun-additions.sh`](../examples/app0/initrun-additions.sh).

```sh
# Create the root passwd/group entries the vendor rootfs lacks.
if ! grep -q '^root:' /etc/passwd 2>/dev/null; then
  echo 'root:x:0:0:root:/root:/bin/sh' >> /etc/passwd
fi
if ! grep -q '^root:' /etc/group 2>/dev/null; then
  echo 'root:x:0:' >> /etc/group
fi

# Remove the vendor profile's rngd message and missing /usr/sbin/psh calls.
sed -i '/^[[:space:]]*\/sbin\/rngd/d' /etc/profile 2>/dev/null
sed -i 's#^[[:space:]]*/usr/sbin/psh.*#:#' /etc/profile 2>/dev/null

# Install the authorized key.
mkdir -p /root/.ssh
chmod 700 /root /root/.ssh
cp /dav/tinyssh/authorized_keys /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Work around the camera's devpts state so PTY sessions work.
if grep -q ' /dev/pts ' /proc/mounts 2>/dev/null; then
  mount -o remount,mode=620,ptmxmode=666 /dev/pts 2>/dev/null
else
  mount -t devpts -o mode=620,ptmxmode=666 devpts /dev/pts 2>/dev/null
fi
[ -e /dev/ptmx ] || ln -s /dev/pts/ptmx /dev/ptmx 2>/dev/null

# Start the inetd listener before waiting for /mnt/mmc01.
/bin/busybox inetd -f -e /dav/inetd.conf >/dev/null 2>&1 &
```

Why before the SD wait: the app0 service then works with the card removed.
The SD hook is only a fallback if the firmware service is missing or not
speaking SSH.

Why patch `/etc/profile`: the vendor profile runs `/sbin/rngd` (a cosmetic
“Initalizing available sources” message) and calls `/usr/sbin/psh`, which does
not exist in the rootfs. Replace the psh lines with `:` rather than deleting
them so the surrounding `if`/`else` branches stay non-empty.

## Build the app image

1. Extract the official package and app filesystem:
   `dav.py unpack` then `dav.py unpack-app`.
2. Add `tinysshd`, `tinyssh/sshkeydir/`, `tinyssh/authorized_keys`, and
   `inetd.conf` to the extracted app tree.
3. Insert the initrun block before the SD wait.
4. Rebuild with `make_firmware.py` and verify:
   `dav.py verify custom.dav --roundtrip`.
5. Install through Local Upgrade and keep the official/recovery images.

The app0 partition is 6 MiB. TinySSH added 98,304 bytes (24 × 4096) to the
SquashFS in the tested image; check the free space reported by
`make_firmware.py` before installing.

See [Firmware](FIRMWARE.md) for the container, build, install, and recovery
procedure.

## Limits

- TinySSH is inetd-style: one process per connection, not a daemon.
- Ed25519 host keys and Ed25519 authorized keys only. No password auth, SCP,
  SFTP, port forwarding, agent forwarding, X11, or compression.
- The kernel is non-multiuser. The patch is required; unpatched builds fail
  during authentication or channel setup.
- The devpts workaround may still leave forced `ssh -tt` with piped stdin
  unable to allocate a controlling terminal. An interactive client TTY works.
  The BusyBox fallback is:
  `ssh -T -p 2222 root@HOST /bin/sh -i`
- Recovery is still not validated beyond app0. Keep the official package and
  a recovery image on separate SD cards.
