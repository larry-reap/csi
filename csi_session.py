"""Bounded CloudShell command transport without secrets in command arguments."""

import base64
import codecs
import contextlib
import os
import re
import selectors
import subprocess
import time
import uuid


class SessionError(Exception):
    """An error whose message is safe to display without request/session data."""


@contextlib.contextmanager
def plugin_process(session, region, interactive=False):
    try:
        version = subprocess.run(
            ['session-manager-plugin', '--version'], capture_output=True,
            text=True, timeout=5, check=True,
        ).stdout.strip()
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
            raise SessionError('Cannot determine Session Manager plugin version')
        if tuple(map(int, version.split('.'))) < (1, 2, 536, 0):
            raise SessionError('Session Manager plugin 1.2.536.0 or newer is required')
    except (OSError, subprocess.SubprocessError):
        raise SessionError('Cannot run Session Manager plugin version check') from None

    import json
    variable = 'AWS_SSM_START_SESSION_RESPONSE'
    env = dict(os.environ)
    env[variable] = json.dumps(session)
    try:
        proc = subprocess.Popen(
            ['session-manager-plugin', variable, region, 'StartSession'],
            env=env, stdin=None if interactive else subprocess.PIPE,
            stdout=None if interactive else subprocess.PIPE,
            # Plugin diagnostics are not a safe channel for session secrets.
            stderr=subprocess.DEVNULL, start_new_session=not interactive,
        )
    except OSError:
        raise SessionError('Cannot start Session Manager plugin') from None
    finally:
        env.pop(variable, None)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()


class CommandChannel:
    def __init__(self, proc, deadline):
        self.proc = proc
        self.deadline = deadline
        self.buffer = b''
        self.escape = b''
        self.selector = selectors.DefaultSelector()
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        os.set_blocking(proc.stdin.fileno(), False)

    def close(self):
        self.selector.close()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SessionError('CloudShell command timed out; session will be closed')
        return remaining

    def send(self, data):
        with selectors.DefaultSelector() as writable:
            writable.register(self.proc.stdin, selectors.EVENT_WRITE)
            while data:
                if not writable.select(self.remaining()):
                    continue
                try:
                    sent = os.write(self.proc.stdin.fileno(), data[:4096])
                except BlockingIOError:
                    continue
                except OSError:
                    raise SessionError('CloudShell input channel closed') from None
                data = data[sent:]

    def until(self, marker, output=None, capture_limit=None):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        captured = b''
        while marker not in self.buffer:
            self.remaining()
            # Keep only the suffix that could contain part of the marker.
            if len(self.buffer) >= len(marker):
                prefix, self.buffer = self.buffer[:-len(marker)], self.buffer[-len(marker):]
                if output is not None:
                    output.write(decoder.decode(prefix))
                    output.flush()
                if capture_limit is not None:
                    captured += prefix
                    if len(captured) > capture_limit:
                        raise SessionError('CloudShell protocol field exceeds its size limit')
            if not self.selector.select(self.remaining()):
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise SessionError('CloudShell session ended before command completion')
            self.buffer += self.plain_terminal_output(chunk)
        prefix, self.buffer = self.buffer.split(marker, 1)
        if output is not None:
            output.write(decoder.decode(prefix, final=True))
            output.flush()
        if capture_limit is not None:
            captured += prefix
            if len(captured) > capture_limit:
                raise SessionError('CloudShell protocol field exceeds its size limit')
            return captured
        return prefix

    def plain_terminal_output(self, chunk):
        # CloudShell's tmux sends cursor/control sequences, often split across
        # WebSocket frames. Remove controls before matching protocol markers.
        output = bytearray()
        for byte in chunk:
            if self.escape:
                self.escape += bytes([byte])
                if self.escape.startswith(b'\x1b['):
                    if len(self.escape) >= 3 and 0x40 <= byte <= 0x7e:
                        self.escape = b''
                elif self.escape.startswith(b'\x1b]'):
                    if byte == 7 or self.escape.endswith(b'\x1b\\'):
                        self.escape = b''
                elif len(self.escape) == 2 and byte in b'()O':
                    continue
                else:
                    self.escape = b''
                if len(self.escape) > 4096:
                    raise SessionError('Invalid terminal control sequence')
            elif byte == 27:
                self.escape = b'\x1b'
            elif byte not in (14, 15):
                output.append(byte)
        return bytes(output)


def execute(session, region, script, output, timeout):
    deadline = time.monotonic() + timeout
    with plugin_process(session, region) as proc:
        channel = CommandChannel(proc, deadline)
        try:
            # No secrets are sent until a successful terminal/history handshake.
            channel.until(b'$')
            ready = 'CSI_READY_' + uuid.uuid4().hex
            bootstrap = (
                "set +x; set +o history; unset HISTFILE; "
                "stty -echo -icanon min 1 time 0 && "
                "{ PS1=; PS2=; printf '\\n%s%s\\n' 'CSI_READY_' '" + ready[10:] + "'; }\n"
            )
            channel.send(bootstrap.encode())
            # Split printf arguments ensure the echoed bootstrap cannot match.
            channel.until((ready + '\r\n').encode())
            marker = 'CSI_DONE_' + uuid.uuid4().hex
            payload = base64.b64encode(script.encode()).decode()
            command = (
                "printf '%s' '" + payload + "' | base64 -d | "
                "env -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS bash --noprofile --norc; "
                "printf '\\n" + marker + "%s\\n' \"$?\"\n"
            )
            channel.send(command.encode())
            channel.until(('\r\n' + marker).encode(), output)
            status = channel.until(b'\r\n', capture_limit=3)
            if not re.fullmatch(rb'\d{1,3}', status) or int(status) > 255:
                raise SessionError('Invalid CloudShell command completion status')
            channel.send(b'exit\n')
            return int(status)
        finally:
            channel.close()
