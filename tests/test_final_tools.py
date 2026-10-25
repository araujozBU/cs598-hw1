"""Offline checks for final-checkpoint inspection, execution, and protocol limits."""

import io
import json
import os
from contextlib import redirect_stderr
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import AgentError, MAX_MODEL_CALLS, PROGRAMMING_PROMPT, Workspace, is_programming_request, model_request, probe_invalid_csv_rows, run_agent
from model_client import ContextLimitError, parse_completion, request_completion
from completion_checks import CompletionChecks


class FinalToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = Workspace(self.root)

    def test_search_finds_late_id_without_loading_all_logs_into_context(self):
        (self.root / 'events.log').write_text('noise\n' * 20000 + 'hold=target worker=inv-w7\ncaused by LockTable\n')
        result = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hold=target'})
        self.assertEqual(result['matched_lines'], [20001])
        self.assertIn('LockTable', json.dumps(result))
        self.assertLess(len(json.dumps(result)), 1000)

    def test_programming_word_in_memory_is_not_a_programming_task(self):
        history = []
        answer = 'I will remember the word implement.'
        def request(messages, deadline):
            self.assertNotEqual(messages[0]['content'], PROGRAMMING_PROMPT)
            return {'content': answer}
        result = run_agent('Remember the word implement for this conversation.',
                           self.workspace, request=request, history=history)
        self.assertEqual(result, {'status': 'success', 'message': answer})
        self.assertEqual(history[-1]['content'], answer)

    def test_programming_word_in_text_filename_does_not_require_python(self):
        self.scripted('Create notes/implement.txt containing exactly Hello.', [
            self.call('write_file', path='notes/implement.txt', content='Hello'),
            self.call('finish', status='success', message='Verified.'),
        ])
        self.assertEqual((self.root / 'notes/implement.txt').read_bytes(), b'Hello')

    def test_explicit_programming_requests_still_select_programming(self):
        for prompt in ['# Task: implement `tally.py`', 'Implement windows.py in this directory.',
                       'Write a command-line tool tally.py.', 'Please create a Python script.',
                       'Build a program that sorts numbers.']:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_programming_request(prompt))

    def test_search_pagination_and_line_ranges(self):
        (self.root / 'events.log').write_text('hit one\nhit two\n' + 'hit more\n' * 19 + 'last\n')
        result = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hit', 'context_lines': 0, 'max_matches': 20})
        self.assertTrue(result['truncated'])
        next_page = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hit', 'start_line': result['next_line']})
        self.assertEqual(next_page['matched_lines'], [21])
        result = self.workspace.execute('read_lines', {'path': 'events.log', 'start_line': 2, 'max_lines': 1})
        self.assertEqual(result['lines'][0]['text'], 'hit two')
        self.assertEqual(result['next_line'], 3)

    def test_search_keeps_late_request_link_and_omits_surrounding_noise(self):
        lines = []
        for i in range(36):
            lines.extend(['noise ' * 50] * 10 + [f'request=target diagnostic={i}'])
        lines.append('request=target downstream=trace-required')
        (self.root / 'events.log').write_text('\n'.join(lines))
        result = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'request=target', 'context_lines': 5})
        self.assertFalse(result['truncated'])
        self.assertTrue(result['context_omitted'])
        self.assertEqual(len(result['matched_lines']), 37)
        self.assertIn('downstream=trace-required', result['lines'][-1]['text'])
        self.assertLess(len(json.dumps(result)), 8000)

    def test_search_pagination_does_not_skip_matches_when_context_exceeds_budget(self):
        (self.root / 'events.log').write_text(('x' * 10000 + '\nhit\n') * 5)
        result = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hit', 'context_lines': 5, 'max_matches': 2})
        self.assertEqual(result['matched_lines'], [2, 4])
        self.assertTrue(result['truncated'])
        next_result = self.workspace.execute('search_file', result['next_search'])
        self.assertEqual(next_result['matched_lines'], [6, 8])
        for number in result['matched_lines']:
            self.assertTrue(any(line['line'] == number and line['text'] == 'hit' for line in result['lines']))

    def test_missing_source_repair_requires_creation(self):
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement app.py to print 7.'},
                    {'role': 'tool', 'content': json.dumps({'create_required': 'app.py'})}]
        with patch('agent.request_completion', return_value={}) as request:
            model_request(messages, time.monotonic() + 10)
        args, kwargs = request.call_args
        self.assertEqual(kwargs['tool_choice']['function']['name'], 'write_file')
        self.assertEqual([tool['function']['name'] for tool in args[1]], ['write_file'])
        self.assertIn('Implement app.py to print 7.', args[0][1]['content'])
        self.assertIn(PROGRAMMING_PROMPT, args[0][0]['content'])
        messages[-1]['content'] = json.dumps({'missing_source_files': ['app.py']})
        with patch('agent.request_completion', return_value={}) as request:
            model_request(messages, time.monotonic() + 10)
        self.assertEqual(request.call_args.kwargs['tool_choice']['function']['name'], 'write_file')

    def test_repair_examples_keep_verified_commands_and_drop_failed_ones(self):
        checks = CompletionChecks(self.workspace, set(), True)
        (self.root / 'app.py').write_text('print(3)')
        arguments = {'path': 'app.py', 'args': ['count'], 'reference_code': 'print(3)'}
        checks.tested(arguments, {'stdout': '3\n', 'exit_code': 0, 'outputs_match': True}, False)
        checks.changed('app.py')
        self.assertEqual(checks.working_examples('app.py'),
                         [{'args': ['count'], 'stdin': '', 'stdout': '3\n'}])
        self.assertEqual(checks.working_examples('other.py'), [])
        checks.tested(arguments, {'stdout': '4\n', 'exit_code': 0, 'outputs_match': False}, True)
        self.assertEqual(checks.working_examples('app.py'), [])

    def test_undeclared_cli_options_fail_source_review_until_repaired(self):
        checks = CompletionChecks(self.workspace, set(), True, programming_prompt='Implement app.py with --region and --until.')
        source = "import argparse\np = argparse.ArgumentParser()\np.add_argument('rest', nargs='*')\na = p.parse_intermixed_args()\n"
        result = checks.source_review(source, 'app.py')
        self.assertEqual(result['missing_options'], ['--region', '--until'])
        self.assertIn('app.py', checks.source_errors)
        fixed = source.replace('a =', "p.add_argument('--region')\np.add_argument('--until')\na =") + 'print(a.region, a.until)\n'
        self.assertIsNone(checks.source_review(fixed, 'app.py'))
        self.assertFalse(checks.source_errors)

    def test_cli_review_allows_dynamic_and_known_argument_parsers(self):
        checks = CompletionChecks(self.workspace, set(), True, programming_prompt='Implement app.py with --region.')
        for source in ["import argparse\np = argparse.ArgumentParser()\np.add_argument('rest')\np.parse_known_args()",
                       "import argparse\np = argparse.ArgumentParser()\nflag = '--region'\np.add_argument(flag)\np.parse_args()"]:
            with self.subTest(source=source):
                self.assertIsNone(checks.source_review(source, 'app.py'))

    def test_unused_greedy_positional_cannot_swallow_optional_value(self):
        checks = CompletionChecks(self.workspace, set(), True, programming_prompt='Implement app.py with --file.')
        source = ("import argparse\np=argparse.ArgumentParser()\np.add_argument('command')\n"
                  "p.add_argument('rest', nargs='*')\np.add_argument('number', nargs='?')\n"
                  "p.add_argument('--file')\na=p.parse_intermixed_args()\nprint(a.command,a.number,a.file)\n")
        self.assertIn('Unused greedy positional', checks.source_review(source, 'app.py')['validation_error'])
        (self.root / 'app.py').write_text(source)
        before = self.workspace.execute('run_python', {'path': 'app.py', 'args': ['top', '3']})
        self.assertEqual(before['stdout'], 'top None None\n')
        fixed = source.replace("p.add_argument('rest', nargs='*')\n", '')
        self.assertIsNone(checks.source_review(fixed, 'app.py'))
        (self.root / 'app.py').write_text(fixed)
        after = self.workspace.execute('run_python', {'path': 'app.py', 'args': ['top', '3']})
        self.assertEqual(after['stdout'], 'top 3 None\n')
        count = self.workspace.execute('run_python', {'path': 'app.py', 'args': ['count']})
        self.assertEqual(count['exit_code'], 0)
        self.assertEqual(count['stdout'], 'count None None\n')
        self.assertIsNone(checks.source_review(source + 'print(a.rest)\n', 'app.py'))

    def test_declared_option_must_be_used_by_command_handler(self):
        checks = CompletionChecks(self.workspace, set(), True, programming_prompt='Implement app.py with --region.')
        source = "import argparse\np = argparse.ArgumentParser()\np.add_argument('--region', dest='region_name')\na = p.parse_args()\nprint('all regions')\n"
        result = checks.source_review(source, 'app.py')
        self.assertEqual(result['unused_options'], {'--region': 'a.region_name'})
        self.assertIsNone(checks.source_review(source.replace("'all regions'", 'a.region_name'), 'app.py'))
        self.assertIsNone(checks.source_review(source.replace("print('all regions')", 'handle_options(a)'), 'app.py'))

    def test_list_positional_cannot_be_converted_as_single_integer(self):
        checks = CompletionChecks(self.workspace, set(), True)
        source = ("import argparse\np=argparse.ArgumentParser()\n"
                  "p.add_argument('command')\np.add_argument('value', nargs='*')\n"
                  "a=p.parse_intermixed_args()\nprint(int(a.value))\n")
        result = checks.source_review(source, 'app.py')
        self.assertIn('is a list', result['validation_error'])
        edit = result['suggested_tool_call']['arguments']
        self.assertEqual(edit['old'], "p.add_argument('value', nargs='*')")
        self.assertEqual(edit['new'], "p.add_argument('value', nargs='?')")
        (self.root / 'app.py').write_text(source)
        self.workspace.execute('replace_text', edit)
        self.assertIsNone(checks.source_review((self.root / 'app.py').read_text(), 'app.py'))
        actual = self.workspace.execute('run_python', {'path': 'app.py', 'args': ['top', '3']})
        self.assertEqual(actual['stdout'], '3\n')
        self.assertIsNone(checks.source_review(source.replace('print(int(a.value))',
                                              'print([int(x) for x in a.value])'), 'app.py'))

    def test_nested_json_integers_reject_boolean_endpoints_at_source_review(self):
        broken = ("import json, sys\npairs = json.loads(sys.stdin.read())\n"
                  "if any(not isinstance(value, int) for pair in pairs for value in pair):\n"
                  "    sys.exit(1)\nprint('[]')\n")
        fixed = broken.replace('isinstance(value, int)', '(type(value) is int)')
        replies = iter([
            self.call('write_file', path='intervals.py', content=broken),
            self.call('replace_text', path='intervals.py', old='isinstance(value, int)',
                      new='(type(value) is int)'),
            self.call('run_python', path='intervals.py', stdin='[[true,3]]', expected_exit_code=1),
            self.call('run_python', path='intervals.py', stdin='[[1,3]]'),
            self.call('finish', status='success', message='Verified.'),
        ])
        history = []
        result = run_agent('Implement intervals.py. Read an array of pairs of JSON integers from stdin. '
                           'Booleans are invalid.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['message'], 'Verified.')
        self.assertEqual((self.root / 'intervals.py').read_text(), fixed)
        self.assertTrue(any('also accepts booleans' in message['content'] for message in history
                            if message['role'] == 'tool'))

    def test_repeated_identical_mismatch_returns_to_repair_instead_of_review(self):
        check = {'outputs_match': False, 'exit_code': 0, 'stdout': '4',
                 'reference': {'exit_code': 0, 'stdout': '3'}, 'repeat_failure': True}
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement app.py.'},
                    self.call('plan_program', interface='CLI', approach='calculate', checks=['output']),
                    {'role': 'tool', 'tool_call_id': 'run', 'content': json.dumps(check)}]
        with patch('agent.request_completion', return_value={'content': 'inspect source'}) as requested:
            model_request(messages, time.monotonic() + 60)
        self.assertIs(requested.call_args.args[0], messages)
        self.assertNotIn('tool_choice', requested.call_args.kwargs)

    def test_runtime_repair_uses_current_source_in_fresh_context(self):
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement app.py.'},
                    {'role': 'assistant', 'content': 'Stale source and failed guesses.'},
                    self.call('run_python', path='app.py'),
                    {'role': 'tool', 'tool_call_id': 'run_python', 'content': json.dumps({
                        'status': 'error', 'repair_required': True, 'stderr': 'UnboundLocalError',
                        'current_source': {'lines': [{'line': 1, 'text': 'print(current_code)'}]}})}]
        with patch('agent.request_completion', return_value=self.call('replace_text', path='app.py', old='a', new='b')) as requested:
            model_request(messages, time.monotonic() + 60)
        repair_messages, repair_tools = requested.call_args.args[:2]
        self.assertEqual(len(repair_messages), 2)
        self.assertNotIn('Stale source', json.dumps(repair_messages))
        self.assertIn('print(current_code)', json.dumps(repair_messages))
        self.assertEqual({tool['function']['name'] for tool in repair_tools}, {'read_lines', 'write_file', 'replace_text'})

    def test_reference_failure_cannot_offer_program_editing_tools(self):
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement app.py.'},
                    self.call('run_python', path='app.py', reference_code='print(missing_import)'),
                    {'role': 'tool', 'tool_call_id': 'run_python', 'content': json.dumps({
                        'outputs_match': False, 'exit_code': 0, 'stdout': '4\n',
                        'reference': {'exit_code': 1, 'stdout': '', 'stderr': 'NameError'}})}]
        with patch('agent.request_completion', return_value={}) as requested:
            model_request(messages, time.monotonic() + 60)
        repair_messages, repair_tools = requested.call_args.args[:2]
        self.assertEqual(len(repair_messages), 2)
        self.assertIn('print(missing_import)', json.dumps(repair_messages))
        self.assertEqual({tool['function']['name'] for tool in repair_tools},
                         {'run_python', 'read_lines', 'read_file', 'analyze_python'})

    def test_runtime_failure_supplies_current_source_to_repair_step(self):
        history = self.scripted('Implement answer.py that prints 4.', [
            self.call('write_file', path='answer.py', content='print(missing_name)'),
            self.call('run_python', path='answer.py'),
            self.call('replace_text', path='answer.py', old='missing_name', new='4'),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        failures = [json.loads(m['content']) for m in history if m['role'] == 'tool' and 'repair_required' in m['content']]
        self.assertTrue(failures[0]['repair_required'])
        self.assertEqual(failures[0]['current_source']['lines'][0]['text'], 'print(missing_name)')

    def test_context_recovery_preserves_task_history_and_failed_check(self):
        calls = 0
        history = [{'role': 'user', 'content': 'My project code is Cedar.'},
                   {'role': 'assistant', 'content': 'I will remember Cedar.'}]
        task = 'Implement answer.py that prints 4.'
        def request(messages, deadline):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.call('write_file', path='answer.py', content='print(5)')
            if calls == 2:
                return self.call('run_python', path='answer.py', reference_code='print(4)')
            if calls == 3:
                raise ContextLimitError('maximum context length')
            if calls == 4:
                self.assertIn('Cedar', json.dumps(messages))
                self.assertIn(task, json.dumps(messages))
                self.assertIn('Current execution state', json.dumps(messages))
                self.assertIn('reference_required', json.dumps(messages))
                return self.call('replace_text', path='answer.py', old='print(5)', new='print(4)')
            if calls == 5:
                return self.call('run_python', path='answer.py', reference_code='print(4)')
            return self.call('finish', status='success', message='Verified.')
        result = run_agent(task, self.workspace, request=request, history=history)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(history[0]['content'], 'My project code is Cedar.')

    def test_partial_shadowing_repair_must_preserve_both_outputs(self):
        source = ('def aggregate(values):\n    return sum(values)\n'
                  'def main():\n    print(aggregate([3, 4]))\n'
                  '    for label, aggregate in [("sample", 9)]:\n'
                  '        print(label, aggregate)\nmain()\n')
        check = {'path': 'answer.py', 'reference_code': 'print(sum([3,4])); print("sample",9)'}
        history = self.scripted('Implement answer.py to print the sum of [3,4], then sample and 9.', [
            self.call('write_file', path='answer.py', content=source),
            self.call('run_python', **check),
            self.call('replace_text', path='answer.py', old='for label, aggregate in', new='for label, value in'),
            self.call('run_python', **check),
            self.call('finish', status='success', message='Partial rename accepted.'),
            self.call('review_mismatch', decision='repair_candidate', reason='The print still uses the global function instead of the loop value.'),
            self.call('replace_text', path='answer.py', old='print(label, aggregate)', new='print(label, value)'),
            self.call('run_python', **check),
            self.call('finish', status='success', message='Verified.'),
        ])
        reviews = [json.loads(m['content']) for m in history if m['role'] == 'tool'
                   and json.loads(m['content']).get('decision') == 'repair_candidate']
        self.assertTrue(reviews[0]['repair_required'])
        self.assertIn('print(label, aggregate)', json.dumps(reviews[0]['current_source']))
        self.assertEqual(self.workspace.execute('run_python', {'path': 'answer.py'})['stdout'], '7\nsample 9\n')

    def test_repeat_detection_uses_source_and_test_not_volatile_output(self):
        self.workspace.write_bytes('answer.py', b'print(lambda: None)')
        checks = CompletionChecks(self.workspace, set(), True)
        arguments = {'path': 'answer.py', 'reference_code': 'print(9)'}
        first = {'stdout': '<function at 0x123>\n', 'stderr': '', 'reference': {'stdout': '9\n', 'stderr': ''}}
        checks.tested(arguments, first, True)
        second = {**first, 'stdout': '<function at 0x456>\n'}
        checks.tested(arguments, second, True)
        self.assertTrue(second['repeat_failure'])
        self.workspace.write_bytes('answer.py', b'print(8)')
        third = {**first, 'stdout': '8\n'}
        checks.tested(arguments, third, True)
        self.assertFalse(third['repeat_failure'])

    def test_rewrite_resets_patch_failures_even_with_remaining_interface_error(self):
        source = "import argparse\np=argparse.ArgumentParser()\na=p.parse_args()\nprint(4)"
        # Include another declaration so static interface review applies.
        source = source.replace('a=p.parse_args()', "p.add_argument('command',nargs='?')\na=p.parse_args()")
        history = self.scripted('Implement answer.py that prints 4 and supports --mode.', [
            self.call('write_file', path='answer.py', content='print(3)'),
            *[self.call('replace_text', path='answer.py', old='absent', new='print(4)') for _ in range(3)],
            self.call('write_file', path='answer.py', content=source),
            self.call('replace_text', path='answer.py', old='a=p.parse_args()',
                      new="p.add_argument('--mode')\na=p.parse_args()\nif a.mode: pass"),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        self.assertIn("p.add_argument('--mode')", (self.root / 'answer.py').read_text())
        errors = [json.loads(m['content']).get('message', '') for m in history if m['role'] == 'tool']
        self.assertFalse(any('Repeated small edits' in error for error in errors))

    def test_failed_patch_preserves_original_runtime_defect(self):
        history = self.scripted('Implement answer.py that prints True.', [
            self.call('write_file', path='answer.py', content="print('abc'.match('abc'))"),
            self.call('run_python', path='answer.py'),
            self.call('replace_text', path='answer.py', old='stale snippet', new='different snippet'),
            self.call('replace_text', path='answer.py', old="'abc'.match('abc')", new="'abc' == 'abc'"),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        errors = [json.loads(m['content']) for m in history if m['role'] == 'tool'
                  and 'original_failure' in json.loads(m['content'])]
        self.assertIn('AttributeError', errors[0]['original_failure']['stderr'])
        self.assertIn('match', errors[0]['original_failure']['stderr'])
        self.assertIn("print('abc'.match('abc'))", json.dumps(errors[0]['current_source']))
        self.assertEqual((self.root / 'answer.py').read_text(), "print('abc' == 'abc')")

    def test_failed_exact_edit_returns_current_source_for_repair(self):
        history = self.scripted('Implement answer.py that prints 4.', [
            self.call('write_file', path='answer.py', content='print(3)'),
            self.call('replace_text', path='answer.py', old='print(2)', new='print(4)'),
            self.call('replace_text', path='answer.py', old='print(3)', new='print(4)'),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        errors = [json.loads(m['content']) for m in history if m['role'] == 'tool' and 'current_source' in m['content']]
        self.assertEqual(errors[0]['current_source']['lines'][0]['text'], 'print(3)')

    def test_fixing_different_syntax_errors_preserves_local_progress(self):
        history = self.scripted('Implement answer.py that prints 4.', [
            self.call('write_file', path='answer.py', content='x =\ny =\nz =\nprint(4)\n'),
            self.call('replace_text', path='answer.py', old='x =\n', new='x = 1\n'),
            self.call('replace_text', path='answer.py', old='y =\n', new='y = 2\n'),
            self.call('replace_text', path='answer.py', old='z =\n', new='z = 3\n'),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        self.assertFalse(any('clean implementation' in m['content'] for m in history if m['role'] == 'tool'))

    def test_large_reads_and_csv_previews_are_bounded(self):
        (self.root / 'large.txt').write_text('a' * 100000)
        result = self.workspace.execute('read_file', {'path': 'large.txt'})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['content']), 8000)
        (self.root / 'boundary.txt').write_text('a' * 40000)
        self.assertEqual(len(self.workspace.execute('read_file', {'path': 'boundary.txt'})['content']), 40000)
        (self.root / 'large.csv').write_text('id,value\n' + '1,2\n' * 40000)
        result = self.workspace.execute('read_csv', {'path': 'large.csv'})
        self.assertEqual(result['row_count'], 40000)
        self.assertEqual(len(result['rows']), 100)
        self.assertTrue(result['truncated'])

    def test_run_generated_program_captures_stdout_stderr_and_exit(self):
        (self.root / 'example.py').write_text('import sys, csv, decimal\nprint(sys.argv[1])\nprint("diagnostic", file=sys.stderr)\n')
        result = self.workspace.execute('run_python', {'path': 'example.py', 'args': ['hello']})
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(result['stdout'], 'hello\n')
        self.assertEqual(result['stderr'], 'diagnostic\n')

    def test_replace_text_preserves_other_bytes_and_rejects_ambiguous_edits(self):
        original = b'# comment\r\nprint(1)\r\n# comment\r\n'
        (self.root / 'example.py').write_bytes(original)
        self.workspace.execute('replace_text', {'path': 'example.py', 'old': 'print(1)', 'new': 'print(2)'})
        expected = original.replace(b'print(1)', b'print(2)')
        self.assertEqual((self.root / 'example.py').read_bytes(), expected)
        for old in ('# comment', 'missing', ''):
            with self.subTest(old=old), self.assertRaises(ValueError):
                self.workspace.execute('replace_text', {'path': 'example.py', 'old': old, 'new': 'oops'})
        self.assertEqual((self.root / 'example.py').read_bytes(), expected)
        changed = self.workspace.execute('replace_text', {'path': 'example.py', 'old': '# comment',
                                                           'new': '# updated', 'replace_all': True})
        self.assertEqual(changed['replacements'], 2)
        self.assertEqual((self.root / 'example.py').read_bytes(), expected.replace(b'# comment', b'# updated'))

    def test_snippet_can_read_inputs_without_exposing_credentials(self):
        (self.root / 'input.txt').write_text('content')
        with patch.dict(os.environ, {'QWEN_API_KEY': 'secret-must-not-reach-child'}):
            result = self.workspace.execute('analyze_python', {'code':
                'import os\nfrom pathlib import Path\nprint(Path("input.txt").read_text())\nprint(os.environ.get("QWEN_API_KEY"))'})
        self.assertEqual(result['stdout'], 'content\nNone\n')
        self.assertEqual(result['exit_code'], 0, result)

    def test_execution_rejects_writes_network_and_session_reads(self):
        (self.root / 'keep.txt').write_text('original')
        (self.root / '.agent_sessions').mkdir()
        (self.root / '.agent_sessions/1.json').write_text('private')
        for code in ['open("keep.txt", "w").write("changed")',
                     'import socket; socket.socket()',
                     'import subprocess; subprocess.run(["echo", "no"])',
                     'print(open(".agent_sessions/1.json").read())',
                     'print(open("/etc/passwd").read())']:
            with self.subTest(code=code):
                result = self.workspace.execute('analyze_python', {'code': code})
                self.assertNotEqual(result['exit_code'], 0, result)
                self.assertIn('PermissionError', result['stderr'])
        self.assertEqual((self.root / 'keep.txt').read_text(), 'original')

    def test_execution_timeout_and_output_limit(self):
        result = self.workspace.execute('analyze_python', {'code': 'while True: pass', 'timeout_seconds': 1})
        self.assertTrue(result['timed_out'])
        result = self.workspace.execute('analyze_python', {'code': 'print("a" * 40000)'})
        self.assertTrue(result['output_truncated'])
        self.assertEqual(len(result['stdout']), 24000)

    def test_stale_session_error_must_recheck_current_file(self):
        (self.root / 'sales.csv').write_text('revenue\n120\n180\n')
        history = [{'role': 'user', 'content': 'Read sales.csv.'},
                   {'role': 'assistant', 'content': 'sales.csv does not exist.'}]
        replies = iter([
            {'content': '{"status":"error","message":"sales.csv does not exist."}'},
            {'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
                'name': 'read_file', 'arguments': '{"path":"sales.csv"}'}}]},
            {'content': 'The total is 300.'},
        ])
        result = run_agent('Read sales.csv and tell me the total.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result, {'status': 'success', 'message': 'The total is 300.'})

    def test_memory_fact_with_file_action_words_stays_conversational(self):
        history = []
        result = run_agent('Remember that my motto is create, read, and write.', self.workspace,
                           request=lambda messages, deadline: {'content': 'Your motto is create, read, and write.'},
                           history=history)
        self.assertEqual(result['message'], 'Your motto is create, read, and write.')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_task_cannot_finish_with_an_unexecuted_plan(self):
        replies = iter([
            {'content': 'I will create answer.txt.'},
            {'tool_calls': [{'id': 'write', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': '{"path":"answer.txt","content":"done"}'}}]},
            {'content': 'Created answer.txt.'},
        ])
        result = run_agent('Create answer.txt.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=[])
        self.assertEqual(result['status'], 'success')
        self.assertEqual((self.root / 'answer.txt').read_text(), 'done')

    def test_model_call_cap(self):
        calls = []
        def request(messages, deadline):
            calls.append(1)
            return {'content': ''}
        with self.assertRaises(AgentError):
            run_agent('Create file.txt.', self.workspace, request=request)
        self.assertEqual(len(calls), MAX_MODEL_CALLS)
        self.assertLessEqual(len(calls), 120)

    def test_program_must_be_tested_again_after_last_edit(self):
        def call(name, arguments):
            return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
        replies = iter([
            call('write_file', {'path': 'answer.py', 'content': 'print(1)'}),
            call('run_python', {'path': 'answer.py'}),
            call('replace_text', {'path': 'answer.py', 'old': 'print(1)', 'new': 'print(2)'}),
            call('analyze_python', {'code': 'print(2)'}),
            call('finish', {'status': 'success', 'message': 'Done too early.'}),
            call('run_python', {'path': 'answer.py'}),
            call('finish', {'status': 'success', 'message': 'Saved program tested.'}),
        ])
        history = []
        result = run_agent('Implement answer.py.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['message'], 'Saved program tested.')
        errors = [json.loads(m['content']) for m in history if m['role'] == 'tool'
                  and json.loads(m['content']).get('status') == 'error']
        self.assertTrue(any('latest edit' in e.get('message', '') for e in errors))

    def test_finish_protocol_retry_and_expected_failure_do_not_poison_completion(self):
        def call(name, arguments):
            return {'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}
        replies = iter([
            {'tool_calls': [call('write_file', {'path': 'check.py', 'content': 'import sys; sys.exit(1)'})]},
            {'tool_calls': [call('run_python', {'path': 'check.py', 'expected_exit_code': 1}),
                            call('finish', {'status': 'success', 'message': 'Premature.'})]},
            {'tool_calls': [call('finish', {'status': 'success', 'message': 'Expected error verified.'})]},
        ])
        result = run_agent('Implement check.py to exit 1.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=[])
        self.assertEqual(result['message'], 'Expected error verified.')

    def test_exact_output_mismatch_is_reported_before_program_repair(self):
        def call(name, arguments):
            return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
        replies = iter([
            call('write_file', {'path': 'answer.py', 'content': 'print("[1, 2]")'}),
            call('run_python', {'path': 'answer.py', 'reference_code': 'print("[1,2]")'}),
            call('analyze_python', {'code': 'print("[1,2]")'}),
            call('write_file', {'path': 'answer.py', 'content': 'print("[1,2]")'}),
            call('run_python', {'path': 'answer.py', 'reference_code': 'print("[1,2]")'}),
            call('finish', {'status': 'success', 'message': 'Fixed.'}),
        ])
        history = []
        result = run_agent('Implement answer.py.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['status'], 'success')
        checks = [json.loads(m['content']) for m in history if m['role'] == 'tool'
                  and 'outputs_match' in json.loads(m['content'])]
        self.assertEqual([c['outputs_match'] for c in checks], [False, True])
        self.assertEqual([c['status'] for c in checks], ['error', 'success'])

    def scripted(self, prompt, steps):
        history = []
        replies = iter(steps)
        def request(messages, deadline):
            try:
                return next(replies)
            except StopIteration:
                self.fail('The agent rejected the completed repair or consumed unexpected model turns.')
        result = run_agent(prompt, self.workspace, request=request, history=history)
        self.assertEqual(result['message'], 'Verified.')
        return history

    @staticmethod
    def call(name, **arguments):
        return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments)}}]}

    def test_mismatch_cannot_be_hidden_by_other_success_or_omitted_reference(self):
        c = self.call
        self.scripted('Implement answer.py that prints 4.', [
            c('write_file', path='answer.py', content='print(5)'),
            c('run_python', path='answer.py', reference_code='print(4)'),
            c('list_files'),
            c('finish', status='success', message='Wrong output accepted.'),
            c('run_python', path='answer.py'),
            {'content': '{"status":"success","message":"Comparison skipped."}'},
            c('replace_text', path='answer.py', old='print(5)', new='print(4)'),
            c('run_python', path='answer.py', reference_code='print(4)'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_incorrect_reference_can_be_repaired_without_changing_program(self):
        c = self.call
        history = self.scripted('Implement answer.py that prints 4.', [
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py', reference_code='print(5)'),
            c('review_mismatch', decision='repair_reference', reason='The task requests 4, but the reference prints 5.'),
            c('run_python', path='answer.py', reference_code='print(4)'),
            c('finish', status='success', message='Verified.'),
        ])
        self.assertEqual((self.root / 'answer.py').read_text(), 'print(4)')
        reviews = [json.loads(message['content']) for message in history if message['role'] == 'tool'
                   and 'repair_reference_required' in json.loads(message['content'])]
        self.assertEqual(reviews[0]['candidate_result']['stdout'], '4\n')
        self.assertEqual(reviews[0]['reference_result']['stdout'], '5\n')

    def test_reference_repair_context_preserves_formatting_mismatch(self):
        review = {'repair_reference_required': True, 'reason': 'The reference prints a Python list, not tabular text.',
                  'failed_test': {'path': 'answer.py', 'args': ['top', '3'],
                                  'reference_code': 'print(rows)'},
                  'candidate_result': {'stdout': 'food\t1.00\n', 'stderr': ''},
                  'reference_result': {'stdout': "[('food', Decimal('1.00'))]\n", 'stderr': ''}}
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement answer.py with tabular top output.'},
                    self.call('review_mismatch', decision='repair_reference', reason=review['reason']),
                    {'role': 'tool', 'tool_call_id': 'review_mismatch', 'content': json.dumps(review)}]
        with patch('agent.request_completion', return_value={}) as requested:
            model_request(messages, time.monotonic() + 5)
        repair_messages, repair_tools = requested.call_args.args[:2]
        repair_context = json.loads(repair_messages[-1]['content'])
        self.assertEqual(repair_context['candidate_result']['stdout'], 'food\t1.00\n')
        self.assertEqual(repair_context['reference_result']['stdout'], "[('food', Decimal('1.00'))]\n")
        self.assertIn('Python list', repair_context['review_reason'])
        self.assertEqual({tool['function']['name'] for tool in repair_tools},
                         {'run_python', 'read_lines', 'read_file', 'analyze_python'})

    def test_programming_mismatch_is_reviewed_in_a_fresh_context(self):
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT},
                    {'role': 'user', 'content': 'Implement answer.py to print 4.'},
                    {'role': 'assistant', 'content': 'Untrusted earlier repair speculation.'},
                    self.call('run_python', path='answer.py', reference_code='print(5)') | {'role': 'assistant'},
                    {'role': 'tool', 'tool_call_id': 'run_python', 'content': json.dumps({
                        'exit_code': 0, 'stdout': '4\n', 'outputs_match': False,
                        'reference': {'exit_code': 0, 'stdout': '5\n'}})}]
        with patch('agent.request_completion', return_value={'content': None}) as requested:
            model_request(messages, time.monotonic() + 5)
        self.assertEqual(requested.call_args.kwargs['tool_choice']['function']['name'], 'review_mismatch')
        self.assertNotIn('Untrusted earlier repair speculation', json.dumps(requested.call_args.args[0]))
        self.assertIn('Implement answer.py to print 4.', requested.call_args.args[0][-1]['content'])

    def test_pending_failures_identify_distinct_stdin_inputs_exactly(self):
        c = self.call
        history = self.scripted('Implement answer.py to print 4.', [
            c('write_file', path='answer.py', content='print(5)'),
            c('run_python', path='answer.py', stdin='first', reference_code='print(4)'),
            c('run_python', path='answer.py', stdin='second', reference_code='print(4)'),
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py', stdin='first', reference_code='print(4)'),
            c('finish', status='success', message='Second input forgotten.'),
            c('run_python', path='answer.py', stdin='second', reference_code='print(4)'),
            c('finish', status='success', message='Verified.'),
        ])
        pending = next(json.loads(message['content'])['pending_checks'] for message in history
                       if message['role'] == 'tool' and 'pending_checks' in json.loads(message['content']))
        self.assertEqual([item['arguments']['stdin'] for item in pending], ['second'])

    def test_failed_command_stays_pending_after_a_different_command_passes(self):
        c = self.call
        self.scripted('Implement answer.py.', [
            c('write_file', path='answer.py', content='import sys; sys.exit(int(sys.argv[1]))'),
            c('run_python', path='answer.py', args=['1']),
            c('run_python', path='answer.py', args=['0']),
            c('finish', status='success', message='Failed command forgotten.'),
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py', args=['1']),
            c('finish', status='success', message='Verified.'),
        ])

    def test_deliberate_error_can_correct_its_expected_exit_code_and_reference(self):
        c = self.call
        self.scripted('Implement answer.py to reject invalid input.', [
            c('write_file', path='answer.py', content='import sys; sys.exit(2)'),
            c('run_python', path='answer.py', reference_code='import sys; sys.exit(2)'),
            c('run_python', path='answer.py', expected_exit_code=2, reference_code='import sys; sys.exit(2)'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_expected_rejection_does_not_require_a_successful_reference(self):
        c = self.call
        self.scripted('Implement answer.py to reject invalid input.', [
            c('write_file', path='answer.py', content='import sys; sys.exit(2)'),
            c('run_python', path='answer.py', expected_exit_code=2,
              reference_code='raise ValueError("reference is broken")'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_programming_inspection_is_not_a_deliverable(self):
        c = self.call
        self.scripted('Implement answer.py that prints 4.', [
            c('list_files'),
            c('finish', status='success', message='Missing program accepted.'),
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_programming_syntax_error_is_returned_immediately_after_write(self):
        c = self.call
        history = self.scripted('Implement answer.py.', [
            c('write_file', path='answer.py', content='print(args.from)'),
            c('finish', status='success', message='Syntax error ignored.'),
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py'),
            c('finish', status='success', message='Verified.'),
        ])
        first_write = next(json.loads(m['content']) for m in history if m['role'] == 'tool')
        self.assertEqual(first_write['status'], 'error')
        self.assertIn('line 1', first_write['syntax_error'])
        self.assertIn('safe argparse dest', first_write['repair_hint'])

    def test_repeated_syntax_breaking_edits_require_a_full_rewrite(self):
        c = self.call
        history = self.scripted('Implement answer.py.', [
            c('write_file', path='answer.py', content='print(4)'),
            c('replace_text', path='answer.py', old='print(4)', new='print('),
            c('replace_text', path='answer.py', old='print(', new='print(1,'),
            c('replace_text', path='answer.py', old='print(1,', new='print(2,'),
            c('replace_text', path='answer.py', old='print(2,', new='print(4)'),
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py'),
            c('finish', status='success', message='Verified.'),
        ])
        self.assertTrue(any('clean implementation' in message['content'] for message in history if message['role'] == 'tool'))

    def test_real_programming_requests_plan_before_writing(self):
        messages = [{'role': 'system', 'content': PROGRAMMING_PROMPT}, {'role': 'user', 'content': 'Implement answer.py.'}]
        with patch('agent.request_completion', return_value={'content': None}) as requested:
            model_request(messages, time.monotonic() + 5)
            self.assertEqual(requested.call_args.kwargs['tool_choice']['function']['name'], 'plan_program')
            messages.append(self.call('plan_program', interface='answer.py', approach='Print a value', checks=['Run it']) | {'role': 'assistant'})
            model_request(messages, time.monotonic() + 5)
            self.assertNotIn('tool_choice', requested.call_args.kwargs)
            messages.append({'role': 'user', 'content': 'Implement another.py.'})
            model_request(messages, time.monotonic() + 5)
            self.assertEqual(requested.call_args.kwargs['tool_choice']['function']['name'], 'plan_program')

    def test_constraint_deliverable_required_for_all_final_response_formats(self):
        c = self.call
        for premature in [c('finish', status='success', message='Missing report accepted.'),
                          {'content': 'Report complete.'},
                          {'content': '{"status":"success","message":"Report complete."}'}]:
            with self.subTest(premature=premature):
                path = self.root / 'findings.json'
                if path.exists():
                    path.unlink()
                self.scripted('CONSTRAINT: Your final deliverable is a file named `findings.json` in the current directory.', [
                    premature,
                    c('write_file', path='findings.json', content='{}'),
                    c('finish', status='success', message='Verified.'),
                ])

    def test_invalid_json_deliverable_is_repaired_before_finish(self):
        c = self.call
        self.scripted('Write findings.json containing a valid JSON object in the current directory.', [
            c('write_file', path='findings.json', content='{broken'),
            c('finish', status='success', message='Invalid JSON accepted.'),
            c('write_file', path='findings.json', content='{"answer":4}'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_exact_text_creation_does_not_infer_json_from_filename(self):
        c = self.call
        self.scripted('Create example.json containing exactly hello.', [
            c('write_file', path='example.json', content='hello'),
            c('finish', status='success', message='Verified.'),
        ])
        self.assertEqual((self.root / 'example.json').read_bytes(), b'hello')

    def test_csv_output_tools_count_as_writing_the_requested_deliverable(self):
        c = self.call
        (self.root / 'source.csv').write_text('id,status\n1,complete\n2,cancelled\n')
        for name, arguments in [
            ('copy_csv', {'path': 'source.csv', 'output_path': 'copy.csv'}),
            ('update_csv', {'path': 'source.csv', 'output_path': 'updated.csv',
                            'id_column': 'id', 'row_id': '1', 'column': 'status', 'value': 'pending'}),
            ('remove_csv_rows', {'path': 'source.csv', 'output_path': 'active.csv',
                                 'column': 'status', 'operator': 'eq', 'value': 'cancelled'}),
        ]:
            with self.subTest(name=name):
                self.scripted('Save ' + arguments['output_path'] + ' using source.csv.', [
                    c(name, **arguments), c('finish', status='success', message='Verified.'),
                ])

    def test_strict_json_integer_array_requires_invalid_type_checks(self):
        c = self.call
        history = self.scripted('Implement answer.py. Read a JSON array of integers. Reject invalid non-integer arrays.', [
            c('write_file', path='answer.py', content='import json, sys; values=json.load(sys.stdin); sys.exit(0 if all(type(x) is int for x in values) else 1)'),
            c('run_python', path='answer.py', stdin='[1,2]'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='[true,1.5,"2"]'),
            c('finish', status='success', message='Mixed invalid types accepted as isolated checks.'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='["2"]'),
            c('finish', status='success', message='Boolean and float checks omitted.'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='[true]'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='[1.5]'),
            c('finish', status='success', message='Verified.'),
        ])
        hints = [json.loads(message['content'])['suggested_tool_call'] for message in history
                 if message['role'] == 'tool' and 'suggested_tool_call' in json.loads(message['content'])]
        self.assertTrue(hints)
        self.assertIs(json.loads(hints[0]['arguments']['stdin'])[0], True)
        self.assertEqual(hints[0]['arguments']['args'], [])

    def test_integer_type_diagnostic_proposes_a_specific_source_repair(self):
        c = self.call
        history = self.scripted('Implement answer.py. Read a JSON array of integers. Reject invalid non-integer arrays.', [
            c('write_file', path='answer.py', content='import json, sys; values=json.load(sys.stdin); sys.exit(0 if all(isinstance(x, int) for x in values) else 1)'),
            c('replace_text', path='answer.py', old='isinstance(x, int)', new='(type(x) is int)'),
            c('run_python', path='answer.py', stdin='[1,2]'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='[true]'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='[1.5]'),
            c('run_python', path='answer.py', expected_exit_code=1, stdin='["2"]'),
            c('finish', status='success', message='Verified.'),
        ])
        first_write = next(json.loads(m['content']) for m in history if m['role'] == 'tool')
        self.assertIn('booleans', first_write['validation_error'])
        self.assertEqual(first_write['suggested_tool_call']['arguments']['new'], '(type(x) is int)')
        checks = CompletionChecks(self.workspace, set(), True, integer_array=True)
        self.assertIsNone(checks.source_review('ok = isinstance(x, int) and not isinstance(x, bool)', 'example.py'))

    def test_scalar_money_zero_case_is_required_and_fixed_precision_is_checked(self):
        c = self.call
        self.scripted('Implement answer.py. All money values use exactly two decimals. '
                      '`python3 answer.py sum` prints the sum.', [
            c('write_file', path='answer.py', content='import sys; print("4.00" if sys.stdin.read().strip() else "0")'),
            c('run_python', path='answer.py', args=['sum'], stdin='4'),
            c('finish', status='success', message='Zero case omitted.'),
            c('run_python', path='answer.py', args=['sum']),
            c('finish', status='success', message='Unformatted zero accepted.'),
            c('replace_text', path='answer.py', old='else "0"', new='else "0.00"'),
            c('run_python', path='answer.py', args=['sum']),
            c('finish', status='success', message='Verified.'),
        ])

    def test_incident_report_requires_all_requested_keys_and_inspected_values(self):
        c = self.call
        (self.root / 'service.log').write_text('trace=trace-blue child=child-green\n')
        self.scripted('Incident: trace a failure using service.log. Write findings.json. '
                      'Copy values exactly from log evidence. Required keys:\n'
                      '1. trace: trace ID.\n2. child: linked child ID.', [
            c('write_file', path='findings.json', content='{"trace":"trace-blue"}'),
            c('finish', status='success', message='Missing key accepted.'),
            c('write_file', path='findings.json', content='{"trace":"trace-blue","child":""}'),
            c('finish', status='success', message='Empty value accepted.'),
            c('write_file', path='findings.json', content='{"trace":"trace-blue","child":"child-green"}'),
            c('finish', status='success', message='Uninspected evidence accepted.'),
            c('search_file', path='service.log', query='trace-blue'),
            c('finish', status='success', message='Verified.'),
        ])

    def test_reference_error_does_not_discard_actual_program_output(self):
        (self.root / 'answer.py').write_text('print(2 + 2)')
        result = self.workspace.execute('run_python', {'path': 'answer.py',
            'reference_code': 'raise ValueError("bad reference")'})
        self.assertEqual(result['stdout'], '4\n')
        self.assertEqual(result['exit_code'], 0)
        self.assertNotEqual(result['reference']['exit_code'], 0)
        self.assertFalse(result['outputs_match'])
        self.assertEqual((self.root / 'answer.py').read_text(), 'print(2 + 2)')

    def test_missing_stdlib_import_in_reference_recovers_without_editing_candidate(self):
        (self.root / 'answer.py').write_text('print(2026)')
        reference = "def year():\n    return datetime.strptime('2026-01-01', '%Y-%m-%d').year\nprint(year())"
        result = self.workspace.execute('run_python', {'path': 'answer.py', 'reference_code': reference})
        self.assertEqual(result['stdout'], '2026\n')
        self.assertTrue(result['outputs_match'])
        self.assertEqual(result['reference_import_added'], 'from datetime import datetime')
        self.assertEqual((self.root / 'answer.py').read_text(), 'print(2026)')

    def test_malformed_csv_probe_blocks_premature_success_and_allows_repair(self):
        (self.root / 'input.csv').write_text('name,amount\nItem,1.00\n')
        broken = ("import csv, sys\nrows = list(csv.reader(open(sys.argv[sys.argv.index('--file') + 1])))\n"
                  "print(len(rows) - 1)\n")
        repaired = 'print(sum(len(row) == len(rows[0]) for row in rows[1:]))'
        replies = iter([
            self.call('write_file', path='count.py', content=broken),
            self.call('run_python', path='count.py', args=['count', '--file', 'input.csv']),
            self.call('finish', status='success', message='Premature.'),
            self.call('replace_text', path='count.py', old='print(len(rows) - 1)', new=repaired),
            self.call('run_python', path='count.py', args=['count', '--file', 'input.csv']),
            self.call('finish', status='success', message='Verified.'),
        ])
        history = []
        result = run_agent('Implement count.py with a count command, --file PATH, and CSV input. '
                           'Skip invalid rows with missing or extra fields.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['message'], 'Verified.')
        self.assertTrue(any('missing-field CSV record' in message['content'] for message in history
                            if message['role'] == 'tool'))

    def test_dictreader_probe_suggests_guard_for_missing_and_extra_cells(self):
        (self.root / 'input.csv').write_text('name,amount\nItem,1.00\n')
        source = ("import csv, sys\nfrom decimal import Decimal\n"
                  "with open(sys.argv[sys.argv.index('--file') + 1]) as handle:\n"
                  "    reader = csv.DictReader(handle)\n    count = 0\n"
                  "    for row in reader:\n        Decimal(row['amount'])\n        count += 1\n"
                  "print(count)\n")
        (self.root / 'count.py').write_text(source)
        result = probe_invalid_csv_rows(self.workspace, 'count.py', 'input.csv')
        self.assertEqual(result['probe_case'], 'missing-field')
        edit = result['suggested_tool_call']['arguments']
        self.workspace.execute('replace_text', edit)
        self.assertEqual(probe_invalid_csv_rows(self.workspace, 'count.py', 'input.csv')['status'], 'success')

    def test_cli_rejection_probe_requires_clean_errors_before_success(self):
        (self.root / 'input.csv').write_text('name\nItem\n')
        broken = ("import sys\npath = sys.argv[sys.argv.index('--file') + 1]\n"
                  "with open(path) as handle:\n    print(len(handle.readlines()) - 1)\n")
        handles_missing = ("import sys\npath = sys.argv[sys.argv.index('--file') + 1]\n"
                           "try:\n    with open(path) as handle:\n        print(len(handle.readlines()) - 1)\n"
                           "except FileNotFoundError as exc:\n"
                           "    print(f'Error: {exc}', file=sys.stderr)\n    sys.exit(1)\n")
        handles_both = handles_missing.replace(
            "path = sys.argv[sys.argv.index('--file') + 1]\n",
            "if sys.argv[1] != 'count':\n"
            "    print('Unsupported command', file=sys.stderr)\n    sys.exit(2)\n"
            "path = sys.argv[sys.argv.index('--file') + 1]\n")
        replies = iter([
            self.call('write_file', path='count.py', content=broken),
            self.call('run_python', path='count.py', args=['count', '--file', 'input.csv']),
            self.call('write_file', path='count.py', content=handles_missing),
            self.call('run_python', path='count.py', args=['count', '--file', 'input.csv']),
            self.call('write_file', path='count.py', content=handles_both),
            self.call('run_python', path='count.py', args=['count', '--file', 'input.csv']),
            self.call('finish', status='success', message='Verified.'),
        ])
        history = []
        result = run_agent('Implement count.py with a count command and --file PATH for CSV input. '
                           'A missing input file or unsupported command must exit nonzero with a useful '
                           'message on stderr and no stdout.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['message'], 'Verified.')
        self.assertEqual((self.root / 'count.py').read_text(), handles_both)
        self.assertTrue(any('missing input file' in message['content'] for message in history
                            if message['role'] == 'tool'))
        self.assertTrue(any('unsupported command' in message['content'] for message in history
                            if message['role'] == 'tool'))

    def test_repeated_broken_reference_fails_before_task_timeout(self):
        replies = iter([
            self.call('write_file', path='answer.py', content='print(4)'),
            *[self.call('run_python', path='answer.py', reference_code='raise ValueError("bad oracle")')
              for _ in range(3)],
        ])
        with self.assertRaisesRegex(AgentError, 'Independent reference failed three times'):
            run_agent('Implement answer.py to print 4.', self.workspace,
                      request=lambda messages, deadline: next(replies), history=[])

    def test_debug_log_summarizes_success_without_null_text_or_source_dump(self):
        replies = iter([
            self.call('write_file', path='answer.py', content='print(4)'),
            self.call('run_python', path='answer.py'),
            self.call('finish', status='success', message='Verified.'),
        ])
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENT_DEBUG': '1'}), redirect_stderr(output):
            run_agent('Implement answer.py to print 4.', self.workspace,
                      request=lambda messages, deadline: next(replies), history=[])
        self.assertNotIn('"text": null', output.getvalue())
        self.assertIn('"stdout": "4\\n"', output.getvalue())
        self.assertNotIn('current_source', output.getvalue())

    def test_explicit_compact_json_contract_rejects_spaced_output(self):
        def call(name, arguments):
            return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
        replies = iter([
            call('write_file', {'path': 'answer.py', 'content': 'print("[1, 2]")'}),
            call('run_python', {'path': 'answer.py'}),
            call('replace_text', {'path': 'answer.py', 'old': '[1, 2]', 'new': '[1,2]'}),
            call('run_python', {'path': 'answer.py'}),
            call('finish', {'status': 'success', 'message': 'Correct format.'}),
        ])
        history = []
        result = run_agent('Implement answer.py. Print a compact JSON array.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['status'], 'success')
        outputs = [json.loads(m['content']) for m in history if m['role'] == 'tool' and 'format_error' in json.loads(m['content'])]
        self.assertEqual([o['status'] for o in outputs], ['error', 'success'])

    def test_repeated_program_failures_require_clean_rewrite(self):
        def call(name, arguments):
            return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
        replies = iter([
            call('write_file', {'path': 'answer.py', 'content': 'raise ValueError("broken")'}),
            *[call('run_python', {'path': 'answer.py'}) for _ in range(3)],
            call('replace_text', {'path': 'answer.py', 'old': 'broken', 'new': 'still broken'}),
            call('write_file', {'path': 'answer.py', 'content': 'print(42)'}),
            call('run_python', {'path': 'answer.py'}),
            call('finish', {'status': 'success', 'message': 'Repaired.'}),
        ])
        history = []
        result = run_agent('Implement answer.py.', self.workspace,
                           request=lambda messages, deadline: next(replies), history=history)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(any('clean implementation' in m['content'] for m in history if m['role'] == 'tool'))

    def test_malformed_tool_arguments_do_not_poison_next_api_request(self):
        calls = 0
        def request(messages, deadline):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'tool_calls': [{'id': 'bad', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': '{"path":"x.txt","content":"bad\\q"}'}}]}
            for message in messages:
                for call in message.get('tool_calls', []):
                    json.loads(call['function']['arguments'])
            if calls == 2:
                self.assertIn('valid JSON', messages[-1]['content'])
                return {'tool_calls': [{'id': 'good', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': '{"path":"x.txt","content":"fixed"}'}}]}
            return {'content': '{"status":"success","message":"Created x.txt."}'}
        result = run_agent('Create x.txt.', self.workspace, request=request, history=[])
        self.assertEqual(result['status'], 'success')
        self.assertEqual((self.root / 'x.txt').read_text(), 'fixed')


class StreamTests(unittest.TestCase):
    def test_context_limit_is_distinguished_from_gateway_failure(self):
        error = urllib.error.HTTPError('https://example.invalid', 400, 'Bad Request', {},
                                      io.BytesIO(b'{"error":{"message":"maximum context length is 40960 tokens"}}'))
        with patch.dict(os.environ, {'QWEN_API_KEY': 'test-key'}), \
                patch('model_client.urllib.request.urlopen', side_effect=error) as opened:
            with self.assertRaises(ContextLimitError):
                request_completion([], [], time.monotonic() + 60)
        self.assertEqual(opened.call_count, 1)

    def test_recovers_from_repeated_gateway_framing_errors_without_echoing_prompts(self):
        errors = [urllib.error.HTTPError('https://example.invalid', 400, 'Bad Request', {},
                  io.BytesIO(b'<html>Bad request syntax (private unrelated prompt)</html>')) for _ in range(3)]
        response = io.BytesIO(b'{"choices":[{"message":{"content":"recovered"},"finish_reason":"stop"}]}')
        response.headers = {'Content-Type': 'application/json'}
        output = io.StringIO()
        with patch.dict(os.environ, {'QWEN_API_KEY': 'test-key', 'AGENT_DEBUG': '1'}), \
                patch('model_client.urllib.request.urlopen', side_effect=[*errors, response]) as opened, \
                patch('model_client.time.sleep'), patch('model_client.sys.stderr', output):
            self.assertEqual(request_completion([], [], time.monotonic() + 60)['content'], 'recovered')
        self.assertEqual(opened.call_count, 4)
        self.assertNotIn('private unrelated prompt', output.getvalue())
        self.assertTrue(all(error.fp.closed for error in errors))

    def test_gateway_retry_respects_deadline(self):
        error = urllib.error.HTTPError('https://example.invalid', 400, 'Bad Request', {},
                                      io.BytesIO(b'<html>Bad request syntax</html>'))
        with patch.dict(os.environ, {'QWEN_API_KEY': 'test-key'}), \
                patch('model_client.urllib.request.urlopen', side_effect=error) as opened, \
                patch('model_client.time.sleep') as sleep:
            with self.assertRaisesRegex(AgentError, 'gateway rejected HTTP request framing'):
                request_completion([], [], time.monotonic() + 1)
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_retries_html_transport_rejection_but_not_regular_http_400(self):
        for transport in (True, False):
            with self.subTest(transport=transport):
                detail = b'<html>Bad request syntax (JSON body)</html>' if transport else b'{"error":"invalid messages"}'
                error = urllib.error.HTTPError('https://example.invalid', 400, 'Bad Request', {}, io.BytesIO(detail))
                response = io.BytesIO(b'{"choices":[{"message":{"content":"hello"},"finish_reason":"stop"}]}')
                response.headers = {'Content-Type': 'application/json'}
                with patch.dict(os.environ, {'QWEN_API_KEY': 'test-key'}), \
                        patch('model_client.urllib.request.urlopen', side_effect=[error, response]) as opened, \
                        patch('model_client.time.sleep'):
                    if transport:
                        self.assertEqual(request_completion([], [], time.monotonic() + 60)['content'], 'hello')
                        self.assertEqual(opened.call_count, 2)
                    else:
                        with self.assertRaises(AgentError):
                            request_completion([], [], time.monotonic() + 60)
                        self.assertEqual(opened.call_count, 1)

    def stream(self, events):
        data = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
        response = io.BytesIO(data.encode())
        response.headers = {'Content-Type': 'text/event-stream'}
        return response

    def test_plain_answer_stream_preserves_fact(self):
        response = self.stream([{'choices': [{'delta': {'content': chunk}, 'finish_reason': None}]}
                                for chunk in ['Your project ', 'is Cedar.']] +
                               [{'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
        self.assertEqual(parse_completion(response)['content'], 'Your project is Cedar.')

    def test_fragmented_parallel_tool_calls(self):
        response = self.stream([
            {'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'id': 'a', 'function': {'name': 'read_file', 'arguments': '{"pa'}},
                {'index': 1, 'id': 'b', 'function': {'name': 'read_file', 'arguments': '{"path":'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [
                {'index': 1, 'function': {'arguments': '"b.txt"}'}},
                {'index': 0, 'function': {'arguments': 'th":"a.txt"}'}}]}, 'finish_reason': 'tool_calls'}]},
        ])
        calls = parse_completion(response)['tool_calls']
        self.assertEqual([json.loads(c['function']['arguments'])['path'] for c in calls], ['a.txt', 'b.txt'])

    def test_incomplete_and_truncated_streams_fail(self):
        for reason in [None, 'length']:
            with self.subTest(reason=reason), self.assertRaises(AgentError):
                parse_completion(self.stream([{'choices': [{'delta': {'content': 'partial'}, 'finish_reason': reason}]}]))

    def test_request_disables_thinking_and_streams(self):
        response = io.BytesIO(b'{"choices":[{"message":{"content":"hello"},"finish_reason":"stop"}]}')
        response.headers = {'Content-Type': 'application/json'}
        with patch.dict(os.environ, {'QWEN_API_KEY': 'test-key'}), patch('model_client.urllib.request.urlopen', return_value=response) as opened:
            result = request_completion([], [], time.monotonic() + 5, temperature=0.7)
        body = json.loads(opened.call_args.args[0].data)
        self.assertTrue(body['stream'])
        self.assertFalse(body['parallel_tool_calls'])
        self.assertEqual(body['temperature'], 0.7)
        self.assertEqual(body['top_p'], 0.8)
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(result['content'], 'hello')


if __name__ == '__main__':
    unittest.main()
