#!/usr/bin/env python3
"""Explicit opt-in entry point for the staged CP4 Hi3861 kernel update.
Without --execute this only checks local preparation artifacts; no network I/O.
"""
import argparse,datetime,hashlib,json,shlex,socket,subprocess,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--image',required=True);p.add_argument('--no-wake',action='store_true');p.add_argument('--protocol',choices=['oem','cp4'],default='oem');p.add_argument('--host',required=True);p.add_argument('--identity')
p.add_argument('--execute',action='store_true',help='actually invoke the MCU updater; requires prior user authorization and continuous external power')
a=p.parse_args()
m=json.loads((HERE/'deployment.json').read_text())
for name,path in [('launcher',HERE/'cp4-hi3861-flash'),('candidate',Path(a.image))]:
 assert hashlib.sha256(path.read_bytes()).hexdigest()==m[name]['sha256'],name+' differs from the live-validated preparation'
proof_bytes=(HERE/'live-probe.txt').read_bytes();proof=proof_bytes.decode();assert 'PROBE_OK' in proof and 'MCU_VERSION_OK version=2.139' in proof and 'FLASH_COMMITTED' not in proof
assert hashlib.sha256(proof_bytes).hexdigest()==m['probe_log_sha256']
if not a.execute:
 print('LOCAL_PREPARATION_OK. No network activity or updater call. Actual execution requires --execute.');raise SystemExit(0)
ssh=['ssh','-p','2222','-o','BatchMode=yes','-o','ConnectTimeout=2','-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2']
if a.identity:ssh+=['-i',a.identity]
ssh+=['root@'+a.host]
# Wake once; readiness polling does not send additional wake packets.
if not a.no_wake:
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:sock.sendto(bytes.fromhex('43503457414b45015003bd2f' if a.protocol=='cp4' else '50030000bd0c2f'),(a.host,50162 if a.protocol=='cp4' else 50161))
end=time.monotonic()+25
while time.monotonic()<end:
 r=subprocess.run(ssh+['echo READY'],capture_output=True,text=True,timeout=6)
 if r.returncode==0:break
 time.sleep(.3)
else:raise SystemExit('No SSH. No updater was launched. Use a cloud or physical wake without an additional sleep hold, then rerun; do not retry if a previous flash was already committed.')
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
remote='/mnt/mmc01/hi3861-wake/flash-'+stamp
command=r'''set -eu
[ "$(md5sum /mnt/mmc01/hi3861-wake/launcher | awk '{print $1}')" = 'HELPER_MD5' ]
[ "$(md5sum /mnt/mmc01/hi3861-wake/candidate.bin | awk '{print $1}')" = 'IMAGE_MD5' ]
[ ! -e /mnt/mmc01/hi3861-wake/launcher.lock ]
trap '' HUP
( /mnt/mmc01/hi3861-wake/launcher -mode flash -execute-flash INSTALL_CP4_HI3861_PREWAKE -log LOG_PATH </dev/null; result=$?; printf '%s\n' "$result" > EXIT_PATH ) > CONSOLE_PATH 2>&1 &
echo REQUEST_LAUNCHED
'''.replace('HELPER_MD5',m['launcher']['md5']).replace('IMAGE_MD5',m['candidate']['md5']).replace('LOG_PATH',shlex.quote(remote+'.log')).replace('EXIT_PATH',shlex.quote(remote+'.exit')).replace('CONSOLE_PATH',shlex.quote(remote+'.console.log'))
# Never retry this launch command: an SSH error after sending it is ambiguous.
report={'host':a.host,'remote_log':remote+'.log','launch_state':'about to send exactly once','candidate_sha256':m['candidate']['sha256'],'boot_success':False}
path=HERE/('flash-request-'+stamp+'.json');path.write_text(json.dumps(report,indent=2)+'\n')
print('Sending flash launch request once. Do not interrupt power or automatically retry. Durable log:',remote+'.log',flush=True)
try:r=subprocess.run(ssh+[command],capture_output=True,text=True,timeout=10)
except subprocess.TimeoutExpired:
 report['launch_state']='ambiguous SSH timeout after sending request';path.write_text(json.dumps(report,indent=2)+'\n')
 print('Ambiguous launch timeout. Inspect the durable log before any further action.');raise SystemExit(3)
report.update(launch_exit_code=r.returncode,launch_stdout=r.stdout,launch_stderr=r.stderr,launch_state='request returned')
path.write_text(json.dumps(report,indent=2)+'\n')
if r.returncode:
 print('Launch response was ambiguous; inspect the durable log before any further action.');raise SystemExit(3)
# Save any progress available before the normal updater takes the network down.
for _ in range(30):
 time.sleep(1)
 try:r=subprocess.run(ssh+['cat '+shlex.quote(remote+'.log')],capture_output=True,text=True,timeout=6)
 except subprocess.TimeoutExpired:break
 if r.returncode:break
 report['last_log']=r.stdout;path.write_text(json.dumps(report,indent=2)+'\n')
 if 'ERROR ' in r.stdout or 'FLASH_WRAPPER_RETURN' in r.stdout:break
print(report.get('last_log','No progress log was retrieved.'))
print('Boot success is unverified. The SD log and a subsequent boot/wake test are required. No automatic second flash or reboot was issued.')
raise SystemExit(3)
