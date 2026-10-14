"""Session persistence tests; scripted model responses require no endpoint calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Workspace, run_agent
from sessions import SESSION_DIRECTORY, SessionStore


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def invoke(self, prompt, session_id):
        # Exercise the real CLI main and disk storage in a NEW Python process.
        # Only the HTTP/model boundary is replaced with predictable responses.
        code = '''
import json, sys
sys.path.insert(0, sys.argv.pop(1))
import agent
def fake_model(messages, deadline):
    requests = [m['content'] for m in messages if m['role'] == 'user']
    if requests[-1].startswith('Remember:'):
        result = {'status': 'success', 'message': 'Noted.'}
    else:
        earlier = [p for p in requests[:-1] if p.startswith('Remember:')]
        result = ({'status': 'success', 'message': earlier[-1]} if earlier else
                  {'status': 'error', 'message': 'No earlier context in this session.'})
    return {'tool_calls': [{'id': 'done', 'type': 'function', 'function': {
        'name': 'finish', 'arguments': json.dumps(result)}}]}
agent.model_request = fake_model
sys.exit(agent.main())
'''
        process = subprocess.run([sys.executable, '-c', code, str(ROOT), prompt, session_id],
                                 cwd=self.root, capture_output=True, text=True,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=10)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertEqual(process.returncode, 0 if result['status'] == 'success' else 1)
        return result

    def test_context_survives_separate_processes(self):
        self.assertEqual(self.invoke('Remember: use sales.csv and revenue.', '1001')['status'], 'success')
        answer = self.invoke('Repeat my earlier instruction.', '1001')
        self.assertEqual(answer['message'], 'Remember: use sales.csv and revenue.')

    def test_interleaved_sessions_are_isolated(self):
        self.invoke('Remember: apples.csv and quantity.', '1001')
        self.invoke('Remember: oranges.csv and price.', '1002')
        self.assertEqual(self.invoke('Repeat it.', '1001')['message'], 'Remember: apples.csv and quantity.')
        self.assertEqual(self.invoke('Repeat it.', '1002')['message'], 'Remember: oranges.csv and price.')
        self.assertEqual(self.invoke('Repeat it.', '1003')['status'], 'error')

    def test_same_id_in_different_workspace_starts_fresh(self):
        self.invoke('Remember: private context.', '1001')
        with tempfile.TemporaryDirectory() as different:
            self.assertEqual(SessionStore(different, '1001').load(), [])

    def test_invalid_id_does_not_create_storage(self):
        for session_id in ['../escape', '', '-1', '1.5', 'abc']:
            with self.subTest(session_id=session_id):
                self.assertEqual(self.invoke('Remember: context.', session_id)['status'], 'error')
        self.assertFalse((self.root / SESSION_DIRECTORY).exists())

    def test_file_tools_cannot_access_any_session_history(self):
        store = SessionStore(self.root, '1001')
        store.save([{'role': 'user', 'content': 'private'}])
        workspace = Workspace(self.root)
        for tool in ['read_file', 'write_file', 'delete_file']:
            args = {'path': SESSION_DIRECTORY + '/1001.json'}
            if tool == 'write_file':
                args['content'] = 'replacement'
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                workspace.execute(tool, args)
        self.assertEqual(store.load(), [{'role': 'user', 'content': 'private'}])

    def test_tool_history_has_no_unanswered_finish_call(self):
        (self.root / 'inventory.txt').write_text('Blue notebooks: 12')
        replies = iter([
            {'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
                'name': 'read_file', 'arguments': '{"path":"inventory.txt"}'}}]},
            {'tool_calls': [{'id': 'finish', 'type': 'function', 'function': {
                'name': 'finish', 'arguments': '{"status":"success","message":"12"}'}}]},
        ])
        history = []
        result = run_agent('Read inventory.txt.', Workspace(self.root),
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['message'], '12')
        self.assertEqual([m['role'] for m in history], ['user', 'assistant', 'tool', 'assistant'])
        self.assertEqual(history[2]['tool_call_id'], 'read')
        self.assertNotIn('tool_calls', history[-1])
        self.assertIn('12', history[2]['content'])

    def test_corrupt_history_fails_without_overwriting_it(self):
        store = SessionStore(self.root, '1001')
        store.path.write_text('{broken')
        self.assertEqual(self.invoke('Remember: replacement.', '1001')['status'], 'error')
        self.assertEqual(store.path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
