"""Offline checks plus four optional, sequential live endpoint tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import AgentError, Workspace, run_agent


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = Workspace(self.root)

    def test_exact_content_and_empty_file(self):
        for name, content in [('nested/"café".txt', 'red\ngreen\nblue'), ('ready.flag', '')]:
            self.workspace.execute('write_file', {'path': name, 'content': content})
            self.assertEqual((self.root / name).read_bytes(), content.encode())

    def test_read_preserves_bytes(self):
        path = self.root / 'notes.txt'
        content = b'Blue notebooks: 12\r\nRed notebooks: 7\r\n'
        path.write_bytes(content)
        result = self.workspace.execute('read_file', {'path': 'notes.txt'})
        self.assertEqual(result['content'], content.decode())
        self.assertEqual(path.read_bytes(), content)

    def test_delete_preserves_neighbors(self):
        (self.root / 'old.txt').write_text('old')
        (self.root / 'keep.txt').write_text('keep')
        self.workspace.execute('delete_file', {'path': 'old.txt'})
        self.assertFalse((self.root / 'old.txt').exists())
        self.assertEqual((self.root / 'keep.txt').read_text(), 'keep')
        with self.assertRaises(ValueError):
            self.workspace.execute('delete_file', {'path': '.'})

    def test_outside_paths_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside).resolve() / 'keep.txt'
            target.write_text('unchanged')
            (self.root / 'link').symlink_to(target.parent, target_is_directory=True)
            for path in [str(target), '../escape.txt', 'link/keep.txt']:
                with self.assertRaises(ValueError):
                    self.workspace.execute('write_file', {'path': path, 'content': 'bad'})
            self.assertEqual(target.read_text(), 'unchanged')

    def test_tool_results_return_to_model(self):
        responses = iter([
            {'tool_calls': [{'id': 'a', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': 'a.txt', 'content': 'hello'})}}]},
            {'tool_calls': [{'id': 'b', 'type': 'function', 'function': {
                'name': 'finish', 'arguments': json.dumps({'status': 'success', 'message': 'Done.'})}}]},
        ])
        def request(messages, deadline):
            if len(messages) > 2:
                self.assertEqual(messages[-1]['role'], 'tool')
                self.assertEqual(json.loads(messages[-1]['content'])['status'], 'success')
            return next(responses)
        result = run_agent('Write hello to a.txt.', self.workspace, request)
        self.assertEqual(result['status'], 'success')
        self.assertEqual((self.root / 'a.txt').read_bytes(), b'hello')

    def test_launcher_from_another_directory_without_key(self):
        env = os.environ.copy()
        env.pop('QWEN_API_KEY', None)
        process = subprocess.run(['bash', str(ROOT / 'run_agent.sh'), 'Create a.txt.'],
                                 cwd=self.root, env=env, capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_text_finish_after_real_operation(self):
        responses = iter([
            {'tool_calls': [{'id': 'a', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': 'a.txt', 'content': 'hello'})}}]},
            {'content': 'finish\n{"status":"success","message":"Done."}'},
        ])
        result = run_agent('Write hello to a.txt.', self.workspace, lambda messages, deadline: next(responses))
        self.assertEqual(result['status'], 'success')
        self.assertEqual((self.root / 'a.txt').read_bytes(), b'hello')

    def test_text_success_without_operation_is_not_accepted(self):
        responses = iter([
            {'content': 'finish\n{"status":"success","message":"Done."}'},
            {'content': '{"status":"error","message":"Unable to complete task."}'},
        ])
        result = run_agent('Write hello to a.txt.', self.workspace, lambda messages, deadline: next(responses))
        self.assertEqual(result['status'], 'error')
        self.assertFalse((self.root / 'a.txt').exists())

    def test_missing_files_and_directory_deletion_preserve_workspace(self):
        (self.root / 'folder').mkdir()
        (self.root / 'folder/keep.txt').write_bytes(b'keep')
        for name, path in [('read_file', 'missing.txt'), ('delete_file', 'missing.txt'),
                           ('delete_file', 'folder')]:
            with self.subTest(name=name, path=path):
                with self.assertRaises((ValueError, OSError)):
                    self.workspace.execute(name, {'path': path})
        self.assertEqual((self.root / 'folder/keep.txt').read_bytes(), b'keep')

    def test_all_operations_reject_outside_paths(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside).resolve() / 'keep.txt'
            target.write_bytes(b'private')
            for name in ['read_file', 'write_file', 'delete_file']:
                args = {'path': str(target)}
                if name == 'write_file':
                    args['content'] = 'overwrite'
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.workspace.execute(name, args)
            self.assertEqual(target.read_bytes(), b'private')

    def test_model_can_recover_from_invalid_arguments(self):
        responses = iter([
            {'tool_calls': [{'id': 'bad', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': '{invalid json'}}]},
            {'tool_calls': [{'id': 'good', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': 'a.txt', 'content': 'fixed'})}}]},
            {'content': '{"status":"success","message":"Done."}'},
        ])
        observed_errors = []
        def request(messages, deadline):
            if messages[-1]['role'] == 'tool':
                observed_errors.append(json.loads(messages[-1]['content'])['status'])
            return next(responses)
        self.assertEqual(run_agent('Create a.txt.', self.workspace, request)['status'], 'success')
        self.assertEqual(observed_errors, ['error', 'success'])
        self.assertEqual((self.root / 'a.txt').read_bytes(), b'fixed')

    def test_nonproductive_model_stops(self):
        with self.assertRaisesRegex(AgentError, 'maximum number'):
            run_agent('Create a.txt.', self.workspace,
                      lambda messages, deadline: {'content': 'I will do that.'})
        self.assertFalse((self.root / 'a.txt').exists())


@unittest.skipUnless(os.environ.get('RUN_LIVE_TESTS') == '1', 'Set RUN_LIVE_TESTS=1 for endpoint tests')
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'unrelated.txt').write_bytes(b'leave me alone\n')

    def run_task(self, prompt, expected_status='success'):
        process = subprocess.run(['bash', str(ROOT / 'run_agent.sh'), prompt],
                                 cwd=self.root, capture_output=True, text=True, timeout=180)
        result = json.loads(process.stdout)
        if os.environ.get('AGENT_DEBUG') == '1':
            print(process.stderr, file=sys.stderr)
        if expected_status == 'success':
            self.assertEqual(process.returncode, 0, result)
        else:
            self.assertNotEqual(process.returncode, 0, result)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertEqual(result['status'], expected_status)
        self.assertIsInstance(result['message'], str)
        self.assertEqual((self.root / 'unrelated.txt').read_bytes(), b'leave me alone\n')
        return result

    def test_create(self):
        self.run_task('Create data/colors.txt containing exactly red, green, and blue on separate lines, with no trailing newline.')
        self.assertEqual((self.root / 'data/colors.txt').read_bytes(), b'red\ngreen\nblue')

    def test_delete(self):
        (self.root / 'old.txt').write_text('obsolete')
        self.run_task('Delete old.txt, leaving other files unchanged.')
        self.assertFalse((self.root / 'old.txt').exists())

    def test_read(self):
        original = b'Blue notebooks: 12\nRed notebooks: 7\n'
        (self.root / 'inventory.txt').write_bytes(original)
        result = self.run_task('Read inventory.txt. How many blue notebooks are currently in stock?')
        self.assertIn('12', result['message'])
        self.assertEqual((self.root / 'inventory.txt').read_bytes(), original)

    def test_summary(self):
        sources = {'start.txt': 'At 09:00 the checkout service stopped working.',
                   'update.txt': 'At 09:15 engineers identified an expired certificate as the cause.',
                   'resolution.txt': 'At 09:30 engineers renewed the certificate and restored checkout.'}
        for name, content in sources.items():
            (self.root / name).write_text(content)
        self.run_task('Read start.txt, update.txt, and resolution.txt. Write reports/summary.txt with fewer than 100 words covering all three times, the cause, and the resolution.')
        summary = (self.root / 'reports/summary.txt').read_text()
        self.assertLess(len(summary.split()), 100)
        for fact in ['09:00', '09:15', '09:30', 'certificate']:
            self.assertIn(fact, summary)
        for name, content in sources.items():
            self.assertEqual((self.root / name).read_text(), content)
        print('Live summary for factual review: ' + summary, file=sys.stderr)


@unittest.skipUnless(os.environ.get('RUN_EXTENDED_LIVE_TESTS') == '1',
                     'Set RUN_EXTENDED_LIVE_TESTS=1 for extra endpoint cases')
class ExtendedLiveTests(unittest.TestCase):
    setUp = LiveTests.setUp
    run_task = LiveTests.run_task

    def test_empty_file(self):
        self.run_task('Make an empty file named ready.flag in the current directory.')
        self.assertEqual((self.root / 'ready.flag').read_bytes(), b'')
        self.assertEqual({p.name for p in self.root.iterdir()}, {'ready.flag', 'unrelated.txt'})

    def test_unicode_quotes_and_spaces(self):
        content = 'Café — "ready" ☕'
        self.run_task('Create the file "meeting notes/café.txt" containing exactly the following text, without a trailing newline:\n' + content)
        self.assertEqual((self.root / 'meeting notes/café.txt').read_bytes(), content.encode())

    def test_selective_nested_deletion(self):
        (self.root / 'archive/2024').mkdir(parents=True)
        for name in ['january.log', 'february.log']:
            (self.root / 'archive/2024' / name).write_bytes(b'original\n')
        self.run_task('Delete archive/2024/january.log, but leave the other log files unchanged.')
        self.assertFalse((self.root / 'archive/2024/january.log').exists())
        self.assertEqual((self.root / 'archive/2024/february.log').read_bytes(), b'original\n')

    def test_answer_multiple_facts(self):
        source = b'Reimbursement is available to full-time employees only. Submit receipts by November 12, 2026. Contractors are not eligible.\n'
        (self.root / 'policy.txt').write_bytes(source)
        result = self.run_task('Based on policy.txt, who is eligible for reimbursement and what is the submission deadline?')
        answer = result['message'].lower()
        for fact in ['full-time', 'november', '12', '2026']:
            self.assertIn(fact, answer)
        self.assertEqual((self.root / 'policy.txt').read_bytes(), source)

    def test_missing_source_returns_error(self):
        self.run_task('According to missing_schedule.txt, when is the final presentation?', expected_status='error')
        self.assertEqual({p.name for p in self.root.iterdir()}, {'unrelated.txt'})

    def test_summary_at_40000_bytes(self):
        facts = [b'The project is named Aurora. Its owner is Priya Shah.\n',
                 b'Aurora has a budget of 84000 dollars.\n',
                 b'Aurora launches on November 18, 2026.\n']
        sources = {}
        filler = b'Routine update: work continues as planned.\n'
        for index, (fact, size) in enumerate(zip(facts, [13333, 13333, 13334])):
            remaining = size - len(fact)
            sources[f'source{index}.txt'] = (filler * (remaining // len(filler) + 1))[:remaining] + fact
        self.assertEqual(sum(map(len, sources.values())), 40000)
        for name, content in sources.items():
            (self.root / name).write_bytes(content)
        self.run_task('Read source0.txt, source1.txt, and source2.txt. Write reports/project.txt in fewer than 70 words stating the project name, owner, budget, and launch date. Ignore routine updates.')
        summary = (self.root / 'reports/project.txt').read_text()
        self.assertLess(len(summary.split()), 70)
        normalized = summary.lower().replace(',', '')
        for fact in ['aurora', 'priya shah', '84000', 'november', '18', '2026']:
            self.assertIn(fact, normalized)
        for name, content in sources.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        print('Large-input summary for factual review: ' + summary, file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
