# app0 TinySSH example

These files are templates for the key-only SSH setup described in
[`docs/APP0-SSH.md`](../../docs/APP0-SSH.md). They contain no keys and no
prebuilt binary.

| File | Use |
| --- | --- |
| `inetd.conf` | Copy to `/dav/inetd.conf` in the app image. |
| `initrun-additions.sh` | Insert into `initrun.sh` after `/dav` is mounted and before the SD wait. |
| `tinyssh-cp4-nonmultiuser.patch` | Apply to the TinySSH source before building. |
| `build-tinyssh.sh` | Optional build template; adjust the toolchain paths. |

Before building the image:

1. Generate your own host key with `tinysshd-makekey -q sshkeydir`.
2. Put your **public** key in `tinyssh/authorized_keys`.
3. Place `tinysshd`, `tinyssh/sshkeydir/`, `tinyssh/authorized_keys`, and
   `inetd.conf` in the extracted app tree as shown in the guide.
4. Insert the initrun block and rebuild the `.dav`.

Never commit or publish `sshkeydir/`, its `.ed25519.sk`, or an app image that
contains them.
