#!/bin/sh
# app0 invokes this SD hook once per Linux boot, after mounting /mnt/mmc01.
# The video server owns the sleep flag; do not combine with an external hold.
set -eu
SD=/mnt/mmc01
awk '$2=="/mnt/mmc01" {found=1} END {exit !found}' /proc/mounts || exit 1
[ "$(md5sum /dav/ezapp | awk '{print $1}')" = 8a4f3c1bf4f850318a17fae01305c20b ] || {
    echo 'cp4-video: unsupported ezapp; refusing startup'
    exit 1
}
[ -x "$SD/cp4-video" ] || exit 1
# Avoid a duplicate listener if this hook is manually invoked again.
if [ -f /tmp/cp4-video.pid ]; then
    old=$(cat /tmp/cp4-video.pid)
    case "$old" in
        ''|*[!0-9]*) ;;
        *) if [ -r "/proc/$old/cmdline" ] &&
              [ "$(tr '\000' '\n' <"/proc/$old/cmdline" | sed -n '1p')" = "$SD/cp4-video" ]; then
               echo "cp4-video: already running ($old)"
               exit 0
           fi ;;
    esac
fi
# app0 starts ezapp asynchronously; wait for its process before launching.
i=0
while ! ps | awk '$NF=="/dav/ezapp" {found=1} END {exit !found}'; do
    i=$((i+1))
    [ "$i" -lt 60 ] || { echo 'cp4-video: ezapp not ready'; exit 1; }
    sleep 1
done
(trap '' HUP; exec "$SD/cp4-video" 8080 0.0.0.0 </dev/null >/tmp/cp4-video.log 2>&1) &
server=$!
echo "$server" > /tmp/cp4-video.pid
sleep 1
kill -0 "$server" || { cat /tmp/cp4-video.log; exit 1; }
echo "cp4-video: started ($server), port 8080; log /tmp/cp4-video.log"
