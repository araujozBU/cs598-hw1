"""End-to-end generation tests against known synthetic inputs and exact outputs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Workspace
from final_fixtures import ALTERNATIVE_PROGRAM, debugging_fixture, programming_fixture


@unittest.skipUnless(os.environ.get('RUN_FINAL_LIVE_TESTS') == '1', 'Set RUN_FINAL_LIVE_TESTS=1 for final-checkpoint tasks')
class FinalLiveTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('KEEP_FINAL_TESTS') == '1':
            self.root = Path(tempfile.mkdtemp(prefix='hw1-final-')).resolve()
            print('Retained test workspace: ' + str(self.root), file=sys.stderr)
        else:
            self.directory = tempfile.TemporaryDirectory()
            self.addCleanup(self.directory.cleanup)
            self.root = Path(self.directory.name).resolve()
        (self.root / 'unrelated/empty').mkdir(parents=True)
        (self.root / 'unrelated/sentinel.bin').write_bytes(b'\x00\xffunchanged')
        self.workspace = Workspace(self.root)

    def invoke(self, prompt):
        (self.root / 'TASK.md').write_text(prompt)
        before = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file()}
        process = subprocess.run(['bash', str(ROOT / 'run_agent.sh'), prompt, '9001'],
                                 cwd=self.root, stdout=subprocess.PIPE,
                                 stderr=None if os.environ.get('AGENT_DEBUG') == '1' else subprocess.PIPE,
                                 text=True, timeout=300)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertTrue(all(isinstance(value, str) for value in result.values()))
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result['status'], 'success', result)
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / path).read_bytes()).hexdigest(), digest, str(path))
        self.assertTrue((self.root / 'unrelated/empty').is_dir())
        print('Final task result: ' + result['message'], file=sys.stderr)

    def run_generated(self, path, args, stdin=''):
        result = self.workspace.execute('run_python', {'path': path, 'args': args, 'stdin': stdin})
        self.assertEqual(result['exit_code'], 0, result)
        self.assertFalse(result['timed_out'])
        self.assertFalse(result['output_truncated'])
        self.assertEqual(result['stderr'], '', result)
        return result['stdout']

    def test_public_programming_requirements(self):
        self.invoke(programming_fixture(self.root))
        cases = [
            (['count'], '40000\n'),
            (['total'], '250000.00\n'),
            (['total', '--category', 'fOoD'], '100000.00\n'),
            (['total', '--category', 'missing'], '0.00\n'),
            (['top', '2'], 'food\t100000.00\ntravel\t100000.00\n'),
            (['top', '9'], 'food\t100000.00\ntravel\t100000.00\nbooks\t50000.00\n'),
            (['range', '--from', '2026-09-02', '--to', '2026-09-03'], 'count=20000 total=97500.00\n'),
            (['range', '--from', '2026-08-01', '--to', '2026-08-02'], 'count=0 total=0.00\n'),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_generated('tally.py', args), expected)
        (self.root / 'custom.csv').write_text('date,description,category,amount\n2026-01-01,"A, B",Other,-3.00\n')
        for args, expected in [(['total', '--file', 'custom.csv'], '-3.00\n'),
                               (['top', '--file', 'custom.csv', '8'], 'other\t-3.00\n'),
                               (['top', '8', '--file', 'custom.csv'], 'other\t-3.00\n')]:
            with self.subTest(args=args):
                self.assertEqual(self.run_generated('tally.py', args), expected)

    def test_programming_generalizes_to_different_task(self):
        self.invoke(ALTERNATIVE_PROGRAM)
        for k, values, expected in [(2, [1, -2, 3, 4], [-1, 1, 7]), (1, [8, 0], [8, 0]),
                                    (5, [1, 2], []), (2, [], []), (3, [5, 6, 7], [18])]:
            with self.subTest(k=k, values=values):
                self.assertEqual(self.run_generated('windows.py', [str(k)], json.dumps(values)),
                                 json.dumps(expected, separators=(',', ':')) + '\n')
        result = self.workspace.execute('run_python', {'path': 'windows.py', 'args': ['0'], 'stdin': '[1,2]'})
        self.assertNotEqual(result['exit_code'], 0)
        self.assertTrue(result['stderr'])

    def test_debugging_follows_ids_and_exact_report_schema(self):
        prompt, expected = debugging_fixture(self.root)
        self.invoke(prompt)
        result = json.loads((self.root / 'REPORT.json').read_text())
        self.assertEqual(result, expected)
        for key, value in expected.items():
            self.assertIs(type(result[key]), type(value), key)

    def test_debugging_alternate_ids_and_root_cause(self):
        prompt, expected = debugging_fixture(self.root, alternate=True)
        self.invoke(prompt)
        result = json.loads((self.root / 'REPORT.json').read_text())
        self.assertEqual(result, expected)
        for key, value in expected.items():
            self.assertIs(type(result[key]), type(value), key)


if __name__ == '__main__':
    unittest.main()
