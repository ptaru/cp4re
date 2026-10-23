#!/usr/bin/env python3
"""Stage and exercise the CP4 launcher WITHOUT calling any firmware updater."""
import argparse,hashlib,json,socket,subprocess,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',required=True);p.add_argument('--identity');p.add_argument('--no-wake',action='store_true');p.add_argument('--protocol',choices=['oem','cp4'],default='oem');p.add_argument('--image',required=True);args=p.parse_args()
ssh=['ssh','-p','2222','-o','BatchMode=yes','-o','ConnectTimeout=2','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2']
if args.identity:ssh+=['-i',args.identity]
ssh+=['root@'+args.host]
def run(cmd,data=None,timeout=15):
 r=subprocess.run(ssh+[cmd],input=data,capture_output=True,timeout=timeout)
 if r.returncode:raise RuntimeError(r.stderr.decode(errors='replace')+'\n'+r.stdout.decode(errors='replace'))
 return r.stdout.decode(errors='replace')
binary=HERE/'cp4-hi3861-flash';image=Path(args.image)
assert hashlib.sha256(image.read_bytes()).hexdigest()=='951c3e569cd794821637263e805105c65b2e576cdd006f9028c4e3aea49679fb'
# One wake only. SSH readiness retries never send further wake packets.
if not args.no_wake:
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.sendto(bytes.fromhex('43503457414b45015003bd2f' if args.protocol=='cp4' else '50030000bd0c2f'),(args.host,50162 if args.protocol=='cp4' else 50161))
setup=r'''set -eu
[ "$(md5sum /dav/ezapp | awk '{print $1}')" = 8a4f3c1bf4f850318a17fae01305c20b ]
pid=$(ps | awk '$NF=="/dav/ezapp" {print $1}')
[ -n "$pid" ]
[ -d /mnt/mmc01 ]
awk '$2=="/mnt/mmc01" {found=1} END {exit !found}' /proc/mounts
[ ! -e /mnt/mmc01/hi3861-wake/launcher.lock ]
current=$(dd if=/proc/$pid/mem bs=4 skip=1688309 count=1 2>/dev/null | hexdump -v -e '1/4 "%u"')
[ "$current" = 1 ]
mkdir -p /mnt/mmc01/hi3861-wake
rm -f /mnt/mmc01/hi3861-wake/prepare.handoff
# Preparation-only hold. Cancelled before the probe obtains its own hold.
trap '' HUP
restore_prepare() { [ -f /mnt/mmc01/hi3861-wake/prepare.handoff ] && return; printf '\001\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null; }
(trap 'exit' TERM INT; trap restore_prepare EXIT
count=0
while [ "$count" -lt 180 ]; do
printf '\000\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null
sleep 1
count=$((count+1))
done
printf '\001\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null) </dev/null >/tmp/hi3861-prepare-rollback.log 2>&1 &
rollback=$!
printf '\000\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null
printf '%s\n' "$rollback" > /mnt/mmc01/hi3861-wake/prepare-rollback.pid
echo PREPARE_HOLD_OK
'''
deadline=time.monotonic()+30;last='' 
while time.monotonic()<deadline:
 try:
  response=run(setup,timeout=8)
  assert 'PREPARE_HOLD_OK' in response
  print(response.strip(),flush=True);break
 except (RuntimeError,subprocess.TimeoutExpired) as e:last=str(e);time.sleep(.3)
else:raise SystemExit('Could not establish preparation hold: '+last)
staged={}
try:
 print(run('if [ -f /mnt/mmc01/hi3861-wake/probe.log ]; then cat /mnt/mmc01/hi3861-wake/probe.log; fi'),flush=True)
 for path,name,mode in [(binary,'launcher','500'),(image,'candidate.bin','400')]:
  run(f"set -eu; cat > /mnt/mmc01/hi3861-wake/{name}.new; chmod {mode} /mnt/mmc01/hi3861-wake/{name}.new; mv /mnt/mmc01/hi3861-wake/{name}.new /mnt/mmc01/hi3861-wake/{name}",path.read_bytes(),timeout=30)
  staged[name]=path.read_bytes()
  print('STAGED',name,hashlib.sha256(staged[name]).hexdigest(),flush=True)
 print(run('/mnt/mmc01/hi3861-wake/launcher -mode check -log /mnt/mmc01/hi3861-wake/preflight.log'),flush=True)
 # Restore transfer hold and start background probe immediately; no updater calls.
 launch=r'''set -eu
pid=$(ps | awk '$NF=="/dav/ezapp" {print $1}')
: > /mnt/mmc01/hi3861-wake/prepare.handoff
kill "$(cat /mnt/mmc01/hi3861-wake/prepare-rollback.pid)"
sleep 2
rm -f /mnt/mmc01/hi3861-wake/probe.exit
if [ -f /mnt/mmc01/hi3861-wake/probe.log ]; then mv /mnt/mmc01/hi3861-wake/probe.log /mnt/mmc01/hi3861-wake/probe.previous.log; fi
trap '' HUP
( /mnt/mmc01/hi3861-wake/launcher -mode probe -adopt-hold -log /mnt/mmc01/hi3861-wake/probe.log </dev/null; result=$?; printf '%s\n' "$result" > /mnt/mmc01/hi3861-wake/probe.exit ) >/mnt/mmc01/hi3861-wake/probe-console.log 2>&1 &
echo PROBE_LAUNCHED
'''
 print(run(launch),flush=True)
 end=time.monotonic()+35
 while time.monotonic()<end:
  status=run('if [ -f /mnt/mmc01/hi3861-wake/probe.exit ]; then cat /mnt/mmc01/hi3861-wake/probe.exit; elif grep -q PROBE_OK /mnt/mmc01/hi3861-wake/probe.log 2>/dev/null; then echo 0; else echo RUNNING; fi',timeout=6).strip()
  if status!='RUNNING':break
  time.sleep(.5)
 else:raise RuntimeError('probe still running; inspect durable probe.log; updater was not called')
 output=run('cat /mnt/mmc01/hi3861-wake/probe.log; cat /mnt/mmc01/hi3861-wake/probe-console.log; cat /proc/uptime',timeout=6)
 (HERE/'live-probe.txt').write_text(output);print(output)
 if status!='0' or 'PROBE_OK' not in output or 'PINNED_IMAGE_READ_OK' not in output:raise RuntimeError('probe failed; see live-probe.txt')
 def ids(body):return {'sha256':hashlib.sha256(body).hexdigest(),'md5':hashlib.md5(body).hexdigest(),'bytes':len(body)}
 deployment={'status':'PREPARATION LIVE-VALIDATED; NO MCU UPDATE EXECUTED','launcher':ids(staged['launcher']),'candidate':ids(staged['candidate.bin']),'probe_log_sha256':hashlib.sha256((HERE/'live-probe.txt').read_bytes()).hexdigest(),'mcu_version_read':'2.139','live_validated':['file and mapping checks','ARM nanosleep attachment','remote mmap','fresh pthread getpid','existing HCC MCU version query','pinned candidate opened inside ezapp using both worker arguments','pthread join','scratch unmap','GPR/VFP/temporary stack restoration checked by readback'],'updater_address':'0x2fd9c','updater_delay_seconds':10,'recovery':'No validated UART/JTAG recovery available'}
 (HERE/'deployment.json').write_text(json.dumps(deployment,indent=2)+'\n')
 print('PREPARATION_PASSED; NO FLASH PERFORMED')
finally:
 # Only the preparation hold is ours to roll back here; the helper owns its hold.
 try:
  run(r'''if [ -f /mnt/mmc01/hi3861-wake/prepare-rollback.pid ]; then
rollback=$(cat /mnt/mmc01/hi3861-wake/prepare-rollback.pid)
if [ ! -f /mnt/mmc01/hi3861-wake/prepare.handoff ] && kill -0 "$rollback" 2>/dev/null; then
kill "$rollback" 2>/dev/null
pid=$(ps | awk '$NF=="/dav/ezapp" {print $1}')
[ -z "$pid" ] || printf '\001\000\000\000' | dd of=/proc/$pid/mem bs=1 seek=6753236 count=4 conv=notrunc 2>/dev/null
fi
rm -f /mnt/mmc01/hi3861-wake/prepare-rollback.pid
fi''',timeout=4)
 except Exception:pass
