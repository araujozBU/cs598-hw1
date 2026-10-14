"""Workspace-local conversation storage, isolated by numeric session ID."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile


SESSION_DIRECTORY = '.agent_sessions'


class SessionStore:
    def __init__(self, workspace, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9]{1,128}', session_id):
            raise ValueError('Session ID must contain 1 to 128 digits.')
        session_id = session_id.lstrip('0') or '0'
        self.directory = Path(workspace).resolve() / SESSION_DIRECTORY
        if self.directory.is_symlink():
            raise ValueError('Session storage must not be a symbolic link.')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / (session_id + '.json')
        self.lock_path = self.directory / (session_id + '.lock')
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError('Session files must not be symbolic links.')

    @contextmanager
    def locked(self):
        # Hold the lock through load, model calls, and save to avoid lost turns.
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self):
        if not self.path.exists():
            return []
        try:
            history = json.loads(self.path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Saved session history is invalid.') from exc
        if not isinstance(history, list) or any(
            not isinstance(message, dict)
            or message.get('role') not in ('user', 'assistant', 'tool')
            for message in history
        ):
            raise ValueError('Saved session history is invalid.')
        return history

    def save(self, history):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.directory, delete=False) as output:
                temporary = Path(output.name)
                json.dump(history, output, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
