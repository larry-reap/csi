import unittest
import time
from unittest import mock

import csi
from csi_session import CommandChannel


class PollingTests(unittest.TestCase):
    def test_startup_reports_only_state_changes(self):
        shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        shell.get_environment_status = mock.Mock(side_effect=[
            {'Status': state} for state in ('CREATING', 'CREATING', 'CREATING', 'RUNNING')
        ])
        with mock.patch.object(csi.time, 'sleep'), self.assertLogs(level='INFO') as logs:
            shell._start_environment('test')
        self.assertEqual(len(logs.output), 2)
        self.assertIn('CREATING', logs.output[0])
        self.assertIn('RUNNING', logs.output[1])

    def test_startup_has_deadline(self):
        shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        shell.get_environment_status = mock.Mock(return_value={'Status': 'CREATING'})
        with mock.patch.object(csi.time, 'monotonic', side_effect=[0, 0, 3]):
            with mock.patch.object(csi.time, 'sleep'):
                with self.assertRaisesRegex(csi.SessionError, 'startup timed out'):
                    shell._start_environment('test', timeout=2)

    def test_heartbeat_context_needs_no_picklable_sdk_session(self):
        shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        stopped_event = []
        def loop(_, stopped):
            stopped_event.append(stopped)
            stopped.wait(5)
        shell._send_heart_beat_loop = loop
        with shell._heart_beat('test'):
            pass
        self.assertEqual(len(stopped_event), 1)
        self.assertTrue(stopped_event[0].is_set())

    def test_split_terminal_controls_do_not_corrupt_markers_or_unicode(self):
        channel = CommandChannel.__new__(CommandChannel)
        channel.escape = b''
        chunks = [b'\x1b[?', b'2004h', '世界'.encode()[:2], '世界'.encode()[2:],
                  b'\x1b]0;window', b'\x1b\\', b'\r\nCSI_DONE_0\r\n']
        result = b''.join(channel.plain_terminal_output(chunk) for chunk in chunks)
        self.assertEqual(result.decode(), '世界\r\nCSI_DONE_0\r\n')

    def test_exit_status_survives_every_read_boundary(self):
        for code in (0, 1, 10, 37, 100, 127, 255):
            payload = str(code).encode() + b'\r\n'
            for split in range(1, len(payload)):
                with self.subTest(code=code, split=split):
                    channel = CommandChannel.__new__(CommandChannel)
                    channel.buffer = b''
                    channel.escape = b''
                    channel.deadline = time.monotonic() + 3
                    channel.proc = mock.Mock()
                    channel.selector = mock.Mock()
                    with mock.patch('csi_session.os.read', side_effect=[payload[:split], payload[split:]]):
                        self.assertEqual(channel.until(b'\r\n', capture_limit=3), str(code).encode())

    def test_cleanup_failure_cannot_report_success(self):
        shell = csi.Cloudshell(session=mock.Mock(region_name='us-east-1'))
        shell._start_environment = mock.Mock()
        shell.create_session = mock.Mock(return_value={'SessionId': 'fake-session'})
        shell.delete_session = mock.Mock(side_effect=RuntimeError('fake-private-token'))
        from contextlib import nullcontext
        shell._heart_beat = mock.Mock(side_effect=lambda _: nullcontext())
        with mock.patch('csi.execute_session', return_value=0), self.assertLogs(level='ERROR') as logs:
            with self.assertRaises(csi.SessionCleanupError):
                shell._execute('fake-environment', 'true')
        self.assertNotIn('fake-private-token', '\n'.join(logs.output))
        with mock.patch('csi.execute_session', return_value=37), self.assertLogs(level='ERROR'):
            self.assertEqual(shell._execute('fake-environment', 'exit 37'), 37)
        with mock.patch('csi.execute_session', side_effect=csi.SessionError('command failed')):
            with self.assertLogs(level='ERROR'):
                with self.assertRaisesRegex(csi.SessionError, 'command failed'):
                    shell._execute('fake-environment', 'false')
