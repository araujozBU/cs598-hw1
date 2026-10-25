"""Reproduce the grading report, including plain-text model answers.

Offline tests replay the reported model behavior in separate processes. Live
tests send the exact reported prompts through run_agent.sh to the configured model.
"""

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
from sessions import SESSION_DIRECTORY


# A retry after a valid plain answer deliberately produces the generic result
# from the report. Thus the old harness fails even without a live endpoint.
REPLAY_PROCESS = '''
import json, sys
sys.path.insert(0, sys.argv.pop(1))
import agent
def replay(messages, deadline):
    latest = messages[-1]
    if latest['role'] == 'user' and latest['content'] == sys.argv[1]:
        if latest['content'].startswith('My project code name is '):
            name = latest['content'].split('My project code name is ', 1)[1].split('. Remember', 1)[0]
            return {'content': "Okay, I'll remember that your project code name is " + name + '.'}
        for message in reversed(messages[:-1]):
            if message['role'] == 'user' and message['content'].startswith('My project code name is '):
                name = message['content'].split('My project code name is ', 1)[1].split('. Remember', 1)[0]
                return {'content': 'The project code name you provided earlier is ' + name + '.'}
        return {'content': 'You have not provided a project code name in this conversation.'}
    return {'content': json.dumps({'status': 'success', 'message': 'Task completed successfully.'})}
agent.model_request = replay
sys.exit(agent.main())
'''


class MemoryCases:
    live = False

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'unrelated/empty').mkdir(parents=True)
        (self.root / 'unrelated/sentinel.bin').write_bytes(b'\x00\xffunchanged\r\n')
        self.initial_files = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.root)): (None if path.is_dir() else path.read_bytes())
                for path in self.root.rglob('*')
                if path.relative_to(self.root).parts[0] != SESSION_DIRECTORY}

    def turn(self, prompt, session_id):
        command = (['bash', str(ROOT / 'run_agent.sh'), prompt, str(session_id)] if self.live else
                   [sys.executable, '-c', REPLAY_PROCESS, str(ROOT), prompt, str(session_id)])
        process = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=180)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertTrue(all(isinstance(value, str) for value in result.values()))
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(self.snapshot(), self.initial_files, 'Task files or directories changed')
        if self.live:
            print(f'Session {session_id}: {result["message"]}', file=sys.stderr)
        return result['message']

    def test_hidden_memory_recall(self):
        self.turn('My project code name is Cedar. Remember it for this conversation.', 6101)
        answer = self.turn('What is my project code name?', 6101)
        self.assertIn('Cedar', answer)

    def test_hidden_session_separation(self):
        self.turn('My project code name is Harbor. Remember it for this conversation.', 6404)
        self.turn('My project code name is Meadow. Remember it for this conversation.', 6405)
        harbor = self.turn('What is my project code name?', 6404)
        meadow = self.turn('What is my project code name?', 6405)
        self.assertIn('Harbor', harbor)
        self.assertNotIn('Meadow', harbor)
        self.assertIn('Meadow', meadow)
        self.assertNotIn('Harbor', meadow)


class MemoryRegressionTests(MemoryCases, unittest.TestCase):
    def test_plain_answers_preserve_quotes_and_unicode(self):
        name = 'Étoile "Blue"'
        self.turn(f'My project code name is {name}. Remember it for this conversation.', 7001)
        answer = self.turn('What is my project code name?', 7001)
        self.assertEqual(answer, f'The project code name you provided earlier is {name}.')
        history = json.loads((self.root / SESSION_DIRECTORY / '7001.json').read_text())
        self.assertEqual(history[-1], {'role': 'assistant', 'content': answer})
        # No artificial user instructions or duplicated answers after finalization.
        self.assertEqual([m['role'] for m in history], ['user', 'assistant', 'user', 'assistant'])

    def test_structured_completion_stores_answer_without_json_envelope(self):
        result = {'status': 'success', 'message': 'Your project code name is Cedar.'}
        for response in [
            {'content': json.dumps(result)},
            {'tool_calls': [{'id': 'done', 'type': 'function', 'function': {
                'name': 'finish', 'arguments': json.dumps(result)}}]},
        ]:
            with self.subTest(response=response):
                history = [{'role': 'user', 'content': 'My project code name is Cedar.'}]
                actual = run_agent('What is my project code name?', Workspace(self.root),
                                   request=lambda messages, deadline: response, history=history)
                self.assertEqual(actual, result)
                self.assertEqual(history[-1], {'role': 'assistant', 'content': result['message']})

    def test_plain_text_cannot_hide_failed_file_operation(self):
        responses = iter([
            {'tool_calls': [{'id': 'missing', 'type': 'function', 'function': {
                'name': 'read_file', 'arguments': '{"path":"missing.txt"}'}}]},
            {'content': 'Task completed successfully.'},
            {'content': '{"status":"error","message":"Could not read missing.txt."}'},
        ])
        result = run_agent('Read missing.txt.', Workspace(self.root),
                           request=lambda messages, deadline: next(responses), history=[])
        self.assertEqual(result['status'], 'error')


@unittest.skipUnless(os.environ.get('RUN_CHECKPOINT2_LIVE_TESTS') == '1',
                     'Set RUN_CHECKPOINT2_LIVE_TESTS=1 for grading-report reproductions')
class MemoryLiveTests(MemoryCases, unittest.TestCase):
    live = True


if __name__ == '__main__':
    unittest.main()
