"""End-to-end generation tests against known synthetic inputs and exact outputs."""

import hashlib
import csv
from decimal import Decimal
import json
import math
import os
import random
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Workspace
from final_fixtures import (ALTERNATIVE_PROGRAM, MERGE_INTERVALS_PROGRAM,
                            PRODUCT_EXCEPT_SELF_PROGRAM, LONGEST_UNIQUE_SUBSTRING_PROGRAM,
                            debugging_fixture, programming_fixture, chained_debugging_fixture,
                            import_pipeline_debugging_fixture)
from final_coverage import assert_tally_coverage, LOCAL_EXTENSIONS


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
        started = time.monotonic()
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
        print(f'Agent elapsed: {time.monotonic() - started:.1f}s', file=sys.stderr)
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
        # Test the generated program against new data it never saw while being
        # written. No additional model calls or guessed hidden requirements.
        for rows in [[],
                     [('2026-01-01', 'One cent, quoted', 'FOOD', '0.01')],
                     [('2026-01-01', 'Purchase\nwith newline', 'Food', '0.10'),
                      ('2026-01-02', 'Refund', 'FOOD', '-0.20'),
                      ('2026-01-02', 'Ride', 'Travel', '-0.10'),
                      ('2026-01-03', 'Gift', 'Books', '0.00')]]:
            with (self.root / 'edge.csv').open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['date', 'description', 'category', 'amount'])
                writer.writerows(rows)
            amounts = [Decimal(row[3]) for row in rows]
            groups = {}
            for row in rows:
                category = row[2].lower()
                groups[category] = groups.get(category, Decimal(0)) + Decimal(row[3])
            top = ''.join(f'{name}\t{amount:.2f}\n' for name, amount in
                          sorted(groups.items(), key=lambda item: (-item[1], item[0])))
            selected = [Decimal(row[3]) for row in rows if row[0] == '2026-01-02']
            cases = [(['count'], f'{len(rows)}\n'),
                     (['total'], f'{sum(amounts, Decimal(0)):.2f}\n'),
                     (['total', '--category', 'fOoD'], f'{groups.get("food", Decimal(0)):.2f}\n'),
                     (['top', '9'], top),
                     (['range', '--from', '2026-01-02', '--to', '2026-01-02'],
                      f'count={len(selected)} total={sum(selected, Decimal(0)):.2f}\n')]
            for args, expected in cases:
                with self.subTest(rows=len(rows), args=args):
                    self.assertEqual(self.run_generated('tally.py', [args[0], '--file', 'edge.csv', *args[1:]]), expected)

        assert_tally_coverage(self, self.root, self.run_generated_cli)

    def run_generated_cli(self, path, args, stdin='', success=True):
        # Direct CLI, no harness execution wrapper. -S excludes site-packages;
        # clear Python search-path overrides and credentials in the child.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('PYTHON', 'OPENAI', 'QWEN'))}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        process = subprocess.run([sys.executable, '-S', str(self.root / path), *args],
                                 cwd=self.root, input=stdin, capture_output=True,
                                 text=True, timeout=30, env=env)
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            self.assertNotEqual(process.returncode, 0)
            self.assertTrue(process.stderr.strip())
            self.assertNotIn('Traceback', process.stderr)
            self.assertEqual(process.stdout, '')
        return process.stdout

    def test_local_programming_extensions_json_and_invalid_rows(self):
        # These semantics are supplied in the prompt; they are NOT predictions
        # of the hidden grading requirements.
        self.invoke(programming_fixture(self.root) + LOCAL_EXTENSIONS)
        rows = [
            ['2026-01-01', 'Purchase', 'Food', '1.25'],
            ['2026-01-02', 'Refund', 'FOOD', '-0.25'],
            ['2026-01-03', 'Train', 'Travel', '2.00'],
            ['2026-02-30', 'Invalid date', 'Food', '99.00'],
            ['bad-date', 'Invalid date', 'Food', '99.00'],
            ['2026-01-01', 'Invalid amount', 'Food', 'NaN'],
            ['2026-01-01', 'Infinite', 'Food', 'Infinity'],
            ['2026-01-01', 'Text', 'Food', 'oops'],
            ['2026-01-01', 'Too precise', 'Food', '1.001'],
            ['2026-01-01', 'No category', '', '99.00'],
            ['2026-01-01', 'Missing field', 'Food'],
            ['2026-01-01', 'Extra field', 'Food', '99.00', 'extra'],
        ]
        with (self.root / 'invalid.csv').open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['date', 'description', 'category', 'amount'])
            writer.writerows(rows)
        checks = [(['count'], '3\n', {'count': 3}),
                  (['total'], '3.00\n', {'total': '3.00'}),
                  (['total', '--category', 'fOoD'], '1.00\n', {'total': '1.00'}),
                  (['total', '--category', 'missing'], '0.00\n', {'total': '0.00'}),
                  (['top', '5'], 'travel\t2.00\nfood\t1.00\n',
                   [{'category': 'travel', 'total': '2.00'}, {'category': 'food', 'total': '1.00'}]),
                  (['range', '--from', '2026-01-01', '--to', '2026-01-02'],
                   'count=2 total=1.00\n', {'count': 2, 'total': '1.00'})]
        for args, plain, structured in checks:
            with self.subTest(args=args):
                self.assertEqual(self.run_generated_cli('tally.py', [*args, '--file', 'invalid.csv']), plain)
                for position in (1, len(args)):
                    invocation = args[:position] + ['--json', '--file', 'invalid.csv'] + args[position:]
                    actual = json.loads(self.run_generated_cli('tally.py', invocation))
                    # JSON canonicalization also distinguishes booleans/floats from integers.
                    self.assertEqual(json.dumps(actual, sort_keys=True), json.dumps(structured, sort_keys=True))
        for args in [['count', '--file', 'absent.csv'], ['unsupported']]:
            with self.subTest(args=args):
                self.run_generated_cli('tally.py', args, success=False)

    def test_programming_generalizes_to_different_task(self):
        self.invoke(ALTERNATIVE_PROGRAM)
        for k, values, expected in [(2, [1, -2, 3, 4], [-1, 1, 7]), (1, [8, 0], [8, 0]),
                                    (5, [1, 2], []), (2, [], []), (3, [5, 6, 7], [18])]:
            with self.subTest(k=k, values=values):
                self.assertEqual(self.run_generated('windows.py', [str(k)], json.dumps(values)),
                                 json.dumps(expected, separators=(',', ':')) + '\n')
        rng = random.Random(598)
        for length in (0, 1, 17, 1000):
            values = [rng.randint(-100000, 100000) for _ in range(length)]
            for k in (1, 3, max(1, length), length + 1):
                expected = [sum(values[i:i+k]) for i in range(max(0, length-k+1))]
                with self.subTest(length=length, k=k):
                    self.assertEqual(self.run_generated_cli('windows.py', [str(k)], json.dumps(values)),
                                     json.dumps(expected, separators=(',', ':')) + '\n')
        result = self.workspace.execute('run_python', {'path': 'windows.py', 'args': ['0'], 'stdin': '[1,2]'})
        self.assertNotEqual(result['exit_code'], 0)
        self.assertTrue(result['stderr'])
        for k, stdin in [('x', '[1,2]'), ('-1', '[1,2]'), ('2', 'not json'),
                         ('2', '{}'), ('2', 'null'), ('2', '42'), ('2', '"text"'), ('2', '[null]'), ('2', '[[]]'), ('2', '[1,"2"]'), ('2', '[true,2]'), ('2', '[1.5,2]')]:
            with self.subTest(k=k, stdin=stdin):
                result = self.workspace.execute('run_python', {'path': 'windows.py', 'args': [k], 'stdin': stdin})
                self.assertNotEqual(result['exit_code'], 0, result)
                self.assertTrue(result['stderr'], result)

    def test_programming_merges_intervals(self):
        self.invoke(MERGE_INTERVALS_PROGRAM)
        cases = [
            ([], []),
            ([[5, 7]], [[5, 7]]),
            ([[1, 3], [2, 6], [8, 10], [15, 18]], [[1, 6], [8, 10], [15, 18]]),
            ([[4, 4], [4, 5], [1, 2]], [[1, 2], [4, 5]]),
            ([[1, 2], [3, 4]], [[1, 2], [3, 4]]),
            ([[-5, -2], [-3, 0], [0, 0], [7, 7]], [[-5, 0], [7, 7]]),
        ]
        rng = random.Random(598)
        for length in (3, 12, 30):
            for _ in range(5):
                intervals = []
                for _ in range(length):
                    start, end = sorted((rng.randint(-20, 20), rng.randint(-20, 20)))
                    intervals.append([start, end])
                rng.shuffle(intervals)
                # An interval graph oracle is deliberately different from the
                # usual sort-and-scan implementation expected from the agent.
                remaining = set(range(len(intervals)))
                merged = []
                while remaining:
                    component = {remaining.pop()}
                    frontier = list(component)
                    while frontier:
                        current = frontier.pop()
                        for other in list(remaining):
                            a, b = intervals[current]
                            c, d = intervals[other]
                            if a <= d and c <= b:
                                remaining.remove(other)
                                component.add(other)
                                frontier.append(other)
                    merged.append([min(intervals[i][0] for i in component),
                                   max(intervals[i][1] for i in component)])
                cases.append((intervals, sorted(merged)))
        for intervals, expected in cases:
            with self.subTest(intervals=intervals):
                self.assertEqual(self.run_generated_cli('merge_intervals.py', [], json.dumps(intervals)),
                                 json.dumps(expected, separators=(',', ':')) + '\n')
        for invalid in ('', 'not json', '{}', 'null', '[1]', '[[1]]', '[[2,1]]',
                        '[[true,3]]', '[[1.5,2]]', '[[1,"2"]]', '[[1,2,3]]', '[[1,2],null]'):
            with self.subTest(invalid=invalid):
                self.run_generated_cli('merge_intervals.py', [], invalid, success=False)

    def test_programming_product_except_self(self):
        self.invoke(PRODUCT_EXCEPT_SELF_PROGRAM)
        cases = [[], [9], [1, 2, 3, 4], [0, 2, 3], [0, 2, 0],
                 [-1, 2, -3, 4], [1, 1, 1, 1], [0], [-7]]
        rng = random.Random(598)
        cases.extend([rng.choices(range(-3, 4), k=length) for length in (2, 5, 12, 30)])
        for values in cases:
            expected = [math.prod(value for index, value in enumerate(values) if index != skip)
                        for skip in range(len(values))]
            with self.subTest(values=values):
                actual = self.run_generated_cli('product_except_self.py', [], json.dumps(values))
                self.assertEqual(actual, json.dumps(expected, separators=(',', ':')) + '\n')
        for invalid in ('', 'not json', '{}', 'null', '42', '[true,2]', '[1.5,2]',
                        '["2",3]', '[1,null]', '[[1],2]'):
            with self.subTest(invalid=invalid):
                self.run_generated_cli('product_except_self.py', [], invalid, success=False)

    def test_programming_longest_unique_substring(self):
        self.invoke(LONGEST_UNIQUE_SUBSTRING_PROGRAM)
        cases = ['', 'a', 'aaaa', 'abcabcbb', 'pwwkew', 'abba', 'dvdf',
                 'AaA', 'éaé', '😀🐍😀x', 'e\u0301é']
        rng = random.Random(598)
        alphabet = ['a', 'b', 'c', 'é', '😀', '\u0301']
        cases.extend(''.join(rng.choices(alphabet, k=length)) for length in (2, 7, 19, 35))
        for value in cases:
            expected = 0
            for start in range(len(value)):
                seen = set()
                for character in value[start:]:
                    if character in seen:
                        break
                    seen.add(character)
                    expected = max(expected, len(seen))
            with self.subTest(value=value):
                self.assertEqual(self.run_generated_cli('longest_unique_substring.py', [],
                                                       json.dumps(value, ensure_ascii=False)), f'{expected}\n')
        for invalid in ('', 'not json', 'null', '42', 'true', '[]', '{}', '["abc"]'):
            with self.subTest(invalid=invalid):
                self.run_generated_cli('longest_unique_substring.py', [], invalid, success=False)

    def test_debugging_import_pipeline_with_successful_storage_branch(self):
        prompt, expected = import_pipeline_debugging_fixture(self.root)
        self.invoke(prompt)
        result = json.loads((self.root / 'REPORT.json').read_text())
        self.assertEqual(result, expected)
        for key, value in expected.items():
            self.assertIs(type(result[key]), type(value), key)

    def test_debugging_follows_ids_and_exact_report_schema(self):
        prompt, expected = debugging_fixture(self.root)
        self.invoke(prompt)
        result = json.loads((self.root / 'REPORT.json').read_text())
        self.assertEqual(result, expected)
        for key, value in expected.items():
            self.assertIs(type(result[key]), type(value), key)

    def test_debugging_chained_worker_and_payment_evidence(self):
        prompt, expected = chained_debugging_fixture(self.root)
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
