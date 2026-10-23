#!/bin/sh
# Build a static 32-bit ARM TinySSH for the CP4 from a source checkout.
#
# This is a template. The tested build used Zig 0.17.0 as a cross compiler;
# adjust ZIG_TARGET/ZIG_CPU or replace the wrapper for your toolchain. See
# ../../docs/APP0-SSH.md for the build record and caveats.
#
# Usage:
#   ./build-tinyssh.sh [source-dir] [output-dir]
#
# The source must be a TinySSH checkout at (or compatible with) the tested
# commit. The non-multiuser patch is applied if it is not already present.
#
# After building, generate a host key and add your public key:
#   ./tinysshd-makekey -q sshkeydir
#   cp ~/.ssh/id_ed25519.pub sshkeydir/../authorized_keys
#
# Never publish sshkeydir/ or an app image that contains it.

set -eu

SRC=${1:-tinyssh}
OUT=${2:-.}
COMMIT=a75c483758d38f3511e867c063a67ae481ca38ff
ZIG_TARGET=arm-linux-musleabihf
ZIG_CPU=cortex_a9
ZIG_FLOAT=hard
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH=${PATCH:-"$HERE/tinyssh-cp4-nonmultiuser.patch"}

if [ ! -d "$SRC" ]; then
    echo "error: source directory not found: $SRC" >&2
    exit 2
fi
if ! command -v zig >/dev/null 2>&1; then
    echo "error: zig not found; install Zig or adapt the CC wrapper" >&2
    exit 2
fi
mkdir -p "$OUT"

cd "$SRC"
if [ -d .git ]; then
    echo "checking out $COMMIT"
    git checkout --detach "$COMMIT"
fi

if ! grep -q "CP4: kernel built without CONFIG_MULTIUSER" dropuidgid.c; then
    echo "applying $PATCH"
    patch -p1 < "$PATCH"
else
    echo "non-multiuser patch already present"
fi

cat > zig-cc-arm.sh <<EOF
#!/bin/sh
exec zig cc -target $ZIG_TARGET -mcpu $ZIG_CPU -mfloat-abi $ZIG_FLOAT "\$@"
EOF
chmod +x zig-cc-arm.sh

# If the feature probe cannot run target binaries, set the HAS* results
# manually; the tested build used:
#   HASLIMITS HASOPENPTY HASLOGINTTY HASMLOCK HASUTMP HASUTMPX
#   (no HASCLOSEFROM/HASLIB25519/HASLIB1305/HASLIBNTRUPRIME/
#    HASLIBRANDOMBYTES/HASVALGRIND)
make clean >/dev/null 2>&1 || true
make tinysshd CC="$PWD/zig-cc-arm.sh" \
  CFLAGS="-W -Wall -Oz -fPIC -fwrapv -Icryptoint -static -fno-pie \
          -fno-unwind-tables -fno-asynchronous-unwind-tables \
          -fno-stack-protector -ffunction-sections -fdata-sections" \
  LDFLAGS="-static -no-pie -s -Wl,--gc-sections"

cp tinysshd "$OUT/tinysshd"
cp tinysshd "$OUT/tinysshd-makekey"
chmod 755 "$OUT/tinysshd" "$OUT/tinysshd-makekey"
echo "wrote $OUT/tinysshd"
file "$OUT/tinysshd" 2>/dev/null || true
