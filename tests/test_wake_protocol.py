"""Exercise CLI packet selection and UDP transport using loopback only."""
import json,socket,subprocess,sys,unittest
from pathlib import Path
TOOL=Path(__file__).resolve().parents[1]/'tools/wake.py'
class ProtocolTests(unittest.TestCase):
 def run_cli(self,*args):
  return subprocess.run([sys.executable,str(TOOL),*args],capture_output=True,text=True,timeout=5)
 def test_one_packet_per_profile(self):
  for profile,payload in [('oem','50030000bd0c2f'),('cp4','43503457414b45015003bd2f')]:
   with self.subTest(profile=profile),socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as receiver:
    receiver.bind(('127.0.0.1',0));receiver.settimeout(.2)
    result=self.run_cli('--host','127.0.0.1','--method','lan','--protocol',profile,'--lan-port',str(receiver.getsockname()[1]),'--wait','0')
    self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(receiver.recv(100),bytes.fromhex(payload))
    with self.assertRaises(socket.timeout):receiver.recv(100)
 def test_default_is_oem(self):
  r=self.run_cli('--host','192.0.2.1','--method','lan','--dry-run');self.assertEqual(r.returncode,0,r.stderr)
  plan=json.loads(r.stdout);self.assertEqual(plan['lan']['target'],'192.0.2.1:50161')
 def test_cp4_hold_and_override(self):
  r=self.run_cli('--host','192.0.2.1','--protocol','cp4','--hold','forever','--dry-run');self.assertEqual(r.returncode,0,r.stderr)
  plan=json.loads(r.stdout);self.assertEqual(plan['lan']['target'],'192.0.2.1:50162');self.assertEqual(plan['lan']['payload_hex'],'43503457414b45015003bd2f');self.assertEqual(plan['hold']['duration'],'forever')
  r=self.run_cli('--host','192.0.2.1','--protocol','cp4','--lan-port','60000','--payload','aabb','--dry-run');plan=json.loads(r.stdout);self.assertEqual(plan['lan']['target'],'192.0.2.1:60000');self.assertEqual(plan['lan']['payload_hex'],'aabb')
 def test_release_does_not_wake(self):
  r=self.run_cli('--host','192.0.2.1','--protocol','cp4','--release-hold','--dry-run');self.assertEqual(r.returncode,0,r.stderr);self.assertFalse(json.loads(r.stdout)['would_wake'])
if __name__=='__main__':unittest.main()
