"""Local-only Session Manager stand-in: relay stdin/stdout through a Bash PTY."""

import json
import os
from pathlib import Path
import pty
import select
import signal
import sys


root = Path(os.environ['FAKE_PLUGIN_DIR'])
if sys.argv[1:] == ['--version']:
    print(os.environ.get('FAKE_PLUGIN_VERSION', '1.2.707.0'))
    sys.exit(int(os.environ.get('FAKE_VERSION_STATUS', '0')))

(root / 'launch.json').write_text(json.dumps({
    'argv': sys.argv,
    'session': os.environ.get('AWS_SSM_START_SESSION_RESPONSE'),
}))
mode = os.environ.get('FAKE_PLUGIN_MODE', 'bash')
if mode == 'eof':
    print('fake-private-plugin-error', file=sys.stderr)
    sys.exit(1)
if mode == 'handshake-failure':
    os.write(1, b'$ ')
    with (root / 'input').open('ab', buffering=0) as received:
        while data := os.read(0, 65536):
            received.write(data)
            os.write(1, data.replace(b'\n', b'\r\n'))
    sys.exit(0)

pid, master = pty.fork()
if pid == 0:
    env = dict(os.environ)
    env.update(PS1='$ ', PS2='> ', HISTFILE=str(root / 'history'),
               FAKE_HISTORY=str(root / 'history'),
               PROMPT_COMMAND='history -w "$FAKE_HISTORY"', TERM='dumb')
    # The session token belongs to the plugin, not the remote shell.
    env.pop('AWS_SSM_START_SESSION_RESPONSE', None)
    os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], env)


def stop(*_):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
try:
    with (root / 'output').open('ab', buffering=0) as output:
        with (root / 'input').open('ab', buffering=0) as received:
            while True:
                readable, _, _ = select.select([0, master], [], [])
                for fd in readable:
                    data = os.read(fd, 65536)
                    if not data:
                        stop()
                    if fd == 0:
                        received.write(data)
                        while data:
                            data = data[os.write(master, data):]
                    else:
                        output.write(data)
                        os.write(1, data)
except OSError:
    stop()
