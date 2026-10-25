"""Read-only local execution guard for generated Python (not a hostile-code sandbox)."""

import io
import json
import os
from pathlib import Path
import resource
import sys
import sysconfig


def main():
    payload = json.load(sys.stdin)
    root = Path(payload['root']).resolve()
    private = root / '.agent_sessions'
    script = payload.get('path', '<agent-snippet>')
    source = payload.get('code')
    if source is None:
        source = Path(script).read_text(encoding='utf-8')
    compiled = compile(source, script, 'exec')
    libraries = {Path(sysconfig.get_path('stdlib')).resolve(), Path(sysconfig.get_path('platstdlib')).resolve()}
    sys.argv = [script, *payload.get('args', [])]
    sys.stdin = io.StringIO(payload.get('stdin', ''))
    sys.path.insert(0, str(root))
    # Bound CPU and output file sizes even if the generated program loops/prints.
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))

    def readable(value):
        if isinstance(value, int):
            return value in (0, 1, 2)
        path = Path(os.fsdecode(value)).resolve()
        if path.is_relative_to(private):
            return False
        return path.is_relative_to(root) or any(path.is_relative_to(lib) for lib in libraries)

    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
            if writing or not readable(path):
                raise PermissionError('Execution is read-only and limited to workspace files and Python libraries.')
        elif event in ('os.listdir', 'os.scandir'):
            if not readable(args[0] if args[0] is not None else str(root)):
                raise PermissionError('Cannot inspect outside the workspace.')
        elif (event.startswith(('socket.', 'subprocess.', 'ctypes.'))
              or event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'os.forkpty',
                           'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.link', 'os.symlink',
                           'os.chmod', 'os.chown', 'os.truncate', 'os.chdir', 'os.utime', 'os.putenv',
                           'os.unsetenv', 'resource.setrlimit')):
            raise PermissionError('Network, subprocesses, and filesystem changes are disabled during execution.')

    sys.addaudithook(audit)
    exec(compiled, {'__name__': '__main__', '__file__': script, '__package__': None})


if __name__ == '__main__':
    main()
