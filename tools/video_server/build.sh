#!/bin/sh
set -eu
cd "$(dirname "$0")"
: "${ZIG:=zig}"
export ZIG_GLOBAL_CACHE_DIR="${ZIG_GLOBAL_CACHE_DIR:-/tmp/cp4-zig-cache}"
export ZIG_LOCAL_CACHE_DIR="${ZIG_LOCAL_CACHE_DIR:-/tmp/cp4-zig-local}"
"$ZIG" cc -target arm-linux-musleabihf -mcpu=cortex_a9 -mfloat-abi=hard \
  -static -Os -Wall -Wextra -Werror -fno-pie -no-pie -s \
  cp4-video.c -o cp4-video-arm
