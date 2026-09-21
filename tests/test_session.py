import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock

import csi_session


SESSION = {'SessionId': 'fake-session', 'TokenValue': 'fake-token-private-93821',
           'StreamUrl': 'wss://example.invalid/fake'}
SECRET = 'fake-script-private-72194'


class LocalTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        plugin = self.root / 'session-manager-plugin'
        fixture = Path(__file__).with_name('fake_session_manager_plugin.py')
        plugin.write_text(f'#!{sys.executable}\n' + fixture.read_text())
        plugin.chmod(0o700)
        self.env = mock.patch.dict(os.environ, {
            'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
            'FAKE_PLUGIN_DIR': str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def execute(self, script, timeout=5):
        output = io.StringIO()
        status = csi_session.execute(SESSION, 'us-east-1', script, output, timeout)
        return status, output.getvalue()

    def read_bytes(self, name):
        path = self.root / name
        return path.read_bytes() if path.exists() else b''

    def test_secret_script_has_no_echo_history_or_process_arguments(self):
        argv_file = self.root / 'shell-argv'
        script = (f"secret='{SECRET}'\n"
                  f'ps -o command= -p "$$,$PPID" > {shlex.quote(str(argv_file))}\n'
                  'printf "safe-result\\n"\n')
        status, output = self.execute(script)
        self.assertEqual(status, 0)
        self.assertIn('safe-result', output)
        launch = json.loads((self.root / 'launch.json').read_text())
        self.assertEqual(json.loads(launch['session']), SESSION)
        self.assertEqual(launch['argv'][1], 'AWS_SSM_START_SESSION_RESPONSE')
        self.assertTrue((self.root / 'history').exists())
        surfaces = [output.encode(), self.read_bytes('output'), self.read_bytes('history'),
                    argv_file.read_bytes(), json.dumps(launch['argv']).encode(),
                    json.dumps(sys.argv).encode()]
        for surface in surfaces:
            self.assertNotIn(SECRET.encode(), surface)
            self.assertNotIn(base64.b64encode(script.encode()), surface)
            self.assertNotIn(SESSION['TokenValue'].encode(), surface)
        self.assertNotIn('AWS_SSM_START_SESSION_RESPONSE', os.environ)

    def test_cli_stdin_uses_real_transport_without_script_in_argv(self):
        import csi
        argv = ['csi', 'execute', 'fake-environment', '--stdin']
        args = csi.make_main_parser().parse_args(argv[1:])
        shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        shell._start_environment = mock.Mock()
        shell._command_session = mock.Mock(return_value=contextlib.nullcontext(SESSION))
        script = f"secret='{SECRET}'\nprintf 'stdin-success\\n'\n"
        output = io.StringIO()
        with mock.patch.object(csi, 'cloudshell', shell), mock.patch.object(sys, 'argv', argv):
            with mock.patch.object(sys, 'stdin', io.StringIO(script)), mock.patch.object(sys, 'stdout', output):
                self.assertEqual(csi.CLI.execute(args), 0)
        self.assertIn('stdin-success', output.getvalue())
        for surface in [self.read_bytes('output'), self.read_bytes('history'),
                        (self.root / 'launch.json').read_bytes()]:
            self.assertNotIn(SECRET.encode(), surface)
            self.assertNotIn(base64.b64encode(script.encode()), surface)

    def test_multiline_script_larger_than_terminal_canonical_buffer(self):
        value = 'abcdefgh' * 2048
        status, output = self.execute(f"value='{value}'\nprintf '%s\\n' \"${{#value}}\"\nprintf 'done\\n'\n")
        self.assertEqual(status, 0)
        self.assertIn('16384\r\ndone\r\n', output)

    def test_nonzero_status_and_unicode_output(self):
        status, output = self.execute("printf 'hello 世界\\n'\nexit 37\n")
        self.assertEqual(status, 37)
        self.assertIn('hello 世界', output)

    def test_command_timeout_is_bounded(self):
        start = time.monotonic()
        with self.assertRaisesRegex(csi_session.SessionError, 'timed out'):
            self.execute('sleep 30\n', timeout=0.5)
        self.assertLess(time.monotonic() - start, 3)

    def test_eof_is_safe_error(self):
        with mock.patch.dict(os.environ, {'FAKE_PLUGIN_MODE': 'eof'}):
            with self.assertRaisesRegex(csi_session.SessionError, 'session ended') as error:
                self.execute(SECRET)
        self.assertNotIn('fake-private-plugin-error', str(error.exception))
        self.assertNotIn(SECRET.encode(), self.read_bytes('input'))

    def test_failed_handshake_never_sends_script(self):
        with mock.patch.dict(os.environ, {'FAKE_PLUGIN_MODE': 'handshake-failure'}):
            with self.assertRaisesRegex(csi_session.SessionError, 'timed out'):
                self.execute(SECRET, timeout=0.4)
        sent = self.read_bytes('input')
        self.assertIn(b'stty', sent)
        self.assertNotIn(SECRET.encode(), sent)
        self.assertNotIn(base64.b64encode(SECRET.encode()), sent)

    def test_unsupported_version_does_not_launch_session(self):
        with mock.patch.dict(os.environ, {'FAKE_PLUGIN_VERSION': '1.2.535.0'}):
            with self.assertRaisesRegex(csi_session.SessionError, 'newer is required'):
                self.execute(SECRET)
        self.assertFalse((self.root / 'launch.json').exists())

    def test_version_error_does_not_show_raw_output(self):
        with mock.patch.dict(os.environ, {'FAKE_PLUGIN_VERSION': SECRET,
                                         'FAKE_VERSION_STATUS': '1'}):
            with self.assertRaises(csi_session.SessionError) as error:
                self.execute(SECRET)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertFalse((self.root / 'launch.json').exists())


class CloudshellLifecycleTests(unittest.TestCase):
    def setUp(self):
        import csi
        self.csi = csi
        self.shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        self.shell._start_environment = mock.Mock()
        self.shell._upload_credentials = mock.Mock()
        self.shell.create_session = mock.Mock(return_value=SESSION)
        self.shell.delete_session = mock.Mock()
        self.shell._heart_beat = mock.Mock(side_effect=lambda _: contextlib.nullcontext())

    def test_ssm_default_does_not_upload_credentials(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(self.csi, 'plugin_process', return_value=contextlib.nullcontext(proc)):
            self.shell._ssm('fake-environment')
        self.shell._upload_credentials.assert_not_called()
        self.shell.delete_session.assert_called_once_with(
            EnvironmentId='fake-environment', SessionId='fake-session')

    def test_plugin_launch_failure_deletes_session(self):
        with mock.patch.object(self.csi, 'plugin_process', side_effect=csi_session.SessionError('failed')):
            with self.assertRaises(csi_session.SessionError):
                self.shell._ssm('fake-environment')
        self.shell._upload_credentials.assert_not_called()
        self.shell.delete_session.assert_called_once_with(
            EnvironmentId='fake-environment', SessionId='fake-session')

    def test_execute_failure_deletes_session(self):
        with mock.patch.object(self.csi, 'execute_session', side_effect=csi_session.SessionError('failed')):
            with self.assertRaises(csi_session.SessionError):
                self.shell._execute('fake-environment', SECRET)
        self.shell._upload_credentials.assert_not_called()
        self.shell.delete_session.assert_called_once_with(
            EnvironmentId='fake-environment', SessionId='fake-session')

    def test_entrypoint_suppresses_raw_error(self):
        with mock.patch.object(self.csi, 'main', side_effect=RuntimeError(SECRET)):
            with self.assertLogs(level='ERROR') as logs:
                self.assertEqual(self.csi.entrypoint(), 1)
        self.assertNotIn(SECRET, '\n'.join(logs.output))
