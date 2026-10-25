"""Offline checks for final-checkpoint inspection, execution, and protocol limits."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import AgentError, MAX_MODEL_CALLS, PROGRAMMING_PROMPT, Workspace, is_programming_request, model_request, run_agent
from model_client import parse_completion, request_completion
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

    def test_declared_option_must_be_used_by_command_handler(self):
        checks = CompletionChecks(self.workspace, set(), True, programming_prompt='Implement app.py with --region.')
        source = "import argparse\np = argparse.ArgumentParser()\np.add_argument('--region', dest='region_name')\na = p.parse_args()\nprint('all regions')\n"
        result = checks.source_review(source, 'app.py')
        self.assertEqual(result['unused_options'], {'--region': 'a.region_name'})
        self.assertIsNone(checks.source_review(source.replace("'all regions'", 'a.region_name'), 'app.py'))
        self.assertIsNone(checks.source_review(source.replace("print('all regions')", 'handle_options(a)'), 'app.py'))

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
        self.scripted('Implement answer.py that prints 4.', [
            c('write_file', path='answer.py', content='print(4)'),
            c('run_python', path='answer.py', reference_code='print(5)'),
            c('review_mismatch', decision='repair_reference', reason='The task requests 4, but the reference prints 5.'),
            c('run_python', path='answer.py', reference_code='print(4)'),
            c('finish', status='success', message='Verified.'),
        ])
        self.assertEqual((self.root / 'answer.py').read_text(), 'print(4)')

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
