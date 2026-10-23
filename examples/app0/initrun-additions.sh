# app0 initrun.sh additions for key-only TinySSH on TCP 2222.
#
# Insert this block after /dav is mounted and BEFORE the SD-card wait, so SSH
# works with the card removed. It is part of the app image's initrun.sh; do
# not run it as a standalone script on your workstation.
#
# Prerequisites in the app image:
#   /dav/tinysshd
#   /dav/tinyssh/sshkeydir/{ed25519.pk,.ed25519.sk}
#   /dav/tinyssh/authorized_keys      (your Ed25519 public key)
#   /dav/inetd.conf
#
# The host key and authorized key are credentials/identifiers. Keep the built
# image private if it contains a host private key.

# The vendor rootfs has /etc/group but no root entry in /etc/passwd.
if ! grep -q '^root:' /etc/passwd 2>/dev/null; then
  echo 'root:x:0:0:root:/root:/bin/sh' >> /etc/passwd
fi
if ! grep -q '^root:' /etc/group 2>/dev/null; then
  echo 'root:x:0:' >> /etc/group
fi

# Silence the vendor login profile. /sbin/rngd prints an "Initalizing
# available sources" line; /usr/sbin/psh does not exist in the rootfs.
# Replace the psh lines with ":" rather than deleting them so the surrounding
# if/else branches stay non-empty.
sed -i '/^[[:space:]]*\/sbin\/rngd/d' /etc/profile 2>/dev/null
sed -i 's#^[[:space:]]*/usr/sbin/psh.*#:#' /etc/profile 2>/dev/null

# Install the authorized key where TinySSH's login shell looks for it.
mkdir -p /root/.ssh
chmod 700 /root /root/.ssh
cp /dav/tinyssh/authorized_keys /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# The camera devpts state needs a remount so PTY sessions work. This is
# runtime-only and reapplied on every boot.
if grep -q ' /dev/pts ' /proc/mounts 2>/dev/null; then
  mount -o remount,mode=620,ptmxmode=666 /dev/pts 2>/dev/null
else
  mount -t devpts -o mode=620,ptmxmode=666 devpts /dev/pts 2>/dev/null
fi
[ -e /dev/ptmx ] || ln -s /dev/pts/ptmx /dev/ptmx 2>/dev/null

# Start the listener before the initrun waits for /mnt/mmc01.
/bin/busybox inetd -f -e /dav/inetd.conf >/dev/null 2>&1 &
