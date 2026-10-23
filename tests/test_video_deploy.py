"""Check deployer packet selection and retries without touching a camera."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / 'tools/video_server/deploy.py'
spec = importlib.util.spec_from_file_location('video_deploy', TOOL)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class VideoDeployTests(unittest.TestCase):
    def test_dry_run_has_no_network_or_binary_dependency(self):
        with patch.object(deploy.socket, 'socket', side_effect=AssertionError('network')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('binary read')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(deploy.main(['--host', '192.0.2.1', '--protocol', 'cp4',
                                          '--port', '9000', '--dry-run']), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan['wake_port'], plan['payload_hex']),
                         (50162, '43503457414b45015003bd2f'))
        self.assertEqual(plan['stream_url'], 'http://192.0.2.1:9000/live.ps')

    def test_packet_profiles_reach_transport(self):
        for protocol, port, payload in [('oem', 50161, '50030000bd0c2f'),
                                       ('cp4', 50162, '43503457414b45015003bd2f')]:
            with self.subTest(protocol=protocol), \
                 patch.object(deploy.socket, 'socket') as sock, \
                 patch.object(Path, 'read_bytes', return_value=b'ARM'), \
                 patch.object(deploy.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, b'ready\n', b'')) as ssh, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deploy.main(['--host', '192.0.2.1', '--protocol', protocol]), 0)
                sock.return_value.__enter__.return_value.sendto.assert_called_once_with(
                    bytes.fromhex(payload), ('192.0.2.1', port))
                self.assertEqual(ssh.call_args.kwargs['input'], b'ARM')

    def test_readiness_retry_does_not_send_another_wake(self):
        responses = [subprocess.CompletedProcess([], 25, b'', b''),
                     subprocess.CompletedProcess([], 0, b'ready\n', b'')]
        with patch.object(deploy.socket, 'socket') as sock, \
             patch.object(Path, 'read_bytes', return_value=b'ARM'), \
             patch.object(deploy.subprocess, 'run', side_effect=responses) as ssh, \
             patch.object(deploy.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            deploy.main(['--host', '192.0.2.1', '--protocol', 'cp4'])
            self.assertEqual(ssh.call_count, 2)
            self.assertEqual(sock.return_value.__enter__.return_value.sendto.call_count, 1)

    def test_authentication_error_is_not_retried(self):
        with patch.object(deploy.socket, 'socket'), \
             patch.object(Path, 'read_bytes', return_value=b'ARM'), \
             patch.object(deploy.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 255, b'', b'Permission denied (publickey)')) as ssh, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'authentication/configuration failed'):
                deploy.main(['--host', '192.0.2.1'])
            ssh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
