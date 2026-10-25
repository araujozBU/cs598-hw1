"""Offline checks for final-checkpoint inspection, execution, and protocol limits."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import AgentError, MAX_MODEL_CALLS, Workspace, run_agent
from model_client import parse_completion, request_completion


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

    def test_search_pagination_and_line_ranges(self):
        (self.root / 'events.log').write_text('hit one\nhit two\n' + 'hit more\n' * 19 + 'last\n')
        result = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hit', 'context_lines': 0})
        self.assertTrue(result['truncated'])
        next_page = self.workspace.execute('search_file', {'path': 'events.log', 'query': 'hit', 'start_line': result['next_line']})
        self.assertEqual(next_page['matched_lines'], [21])
        result = self.workspace.execute('read_lines', {'path': 'events.log', 'start_line': 2, 'max_lines': 1})
        self.assertEqual(result['lines'][0]['text'], 'hit two')
        self.assertEqual(result['next_line'], 3)

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
        self.assertEqual([c['status'] for c in checks], ['success', 'success'])

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
