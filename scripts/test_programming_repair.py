"""Live diagnostics for seeded shadowing, reference, and CLI parser defects.

Uses normal model environment variables. This isolates repair behavior; it is
not a substitute for the full generation tests in tests/test_final_live.py.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent


def call(name, **arguments):
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['shadowing', 'reference', 'parser'], default='shadowing')
    case = parser.parse_args().case
    source = ('def aggregate(values):\n    return sum(values)\n\ndef main():\n'
              '    print(aggregate([3, 4]))\n'
              '    for label, aggregate in [("sample", 9)]:\n'
              '        print(label, aggregate)\n\nmain()\n')
    task = ('Implement example.py to sum [3,4], print that sum, then print sample and 9 '
            'separated by a space on the next line. Preserve both operations.')
    reference_code = 'print(sum([3,4])); print("sample",9)'
    expected = '7\nsample 9\n'
    if case == 'reference':
        source = 'print(2026)\n'
        task = 'Implement example.py to print the year of January 1, 2026.'
        reference_code = 'print(datetime.date(2026, 1, 1).year)'
        expected = '2026\n'
    test_args = []
    if case == 'parser':
        source = ("import argparse\np=argparse.ArgumentParser()\n"
                  "p.add_argument('command',choices=['count','top'])\n"
                  "p.add_argument('args',nargs='*')\np.add_argument('N',nargs='?',type=int)\n"
                  "a=p.parse_intermixed_args()\n"
                  "if a.command == 'count': print(3)\n"
                  "elif a.N is None: p.error('N is required for top')\n"
                  "else: print(' '.join(str(x) for x in [9,7,2][:a.N]))\n")
        task = ('Implement example.py. count prints 3 without additional arguments. '
                'top N prints the first N values from [9,7,2] separated by spaces. '
                'N must only be required for top. Preserve both commands.')
        test_args = ['top', '2']
        reference_code = 'print("9 7")'
        expected = '9 7\n'
    seed = iter([
        call('plan_program', interface='Run example.py', approach='Implement the task and preserve every specified output and command.',
             checks=['Exact output comparison', 'Preserve other commands when applicable']),
        call('write_file', path='example.py', content=source),
        call('run_python', path='example.py', args=test_args, reference_code=reference_code),
    ])
    real_calls = 0

    def request(messages, deadline):
        nonlocal real_calls
        try:
            return next(seed)
        except StopIteration:
            pass
        real_calls += 1
        response = agent.model_request(messages, deadline)
        print('Live repair step', real_calls,
              [c['function']['name'] for c in response.get('tool_calls', [])], flush=True)
        return response

    agent.MAX_MODEL_CALLS = 15
    agent.TIME_LIMIT_SECONDS = 120
    with tempfile.TemporaryDirectory(prefix='hw1-repair-loop-') as directory:
        workspace = agent.Workspace(Path(directory))
        started = time.monotonic()
        result = agent.run_agent(task, workspace, request=request, history=[])
        actual = workspace.execute('run_python', {'path': 'example.py', 'args': test_args})
        if result['status'] != 'success' or actual['exit_code'] != 0 or actual['stdout'] != expected:
            raise AssertionError({'result': result, 'program': actual})
        if case == 'parser':
            count = workspace.execute('run_python', {'path': 'example.py', 'args': ['count']})
            if count['exit_code'] != 0 or count['stdout'] != '3\n':
                raise AssertionError({'regressed_count': count})
        if case == 'reference' and workspace.file_path('example.py').read_text() != source:
            raise AssertionError('The valid candidate was changed to repair a broken reference.')
        print(f'PASS: {case} repair verified using {real_calls} live calls in {time.monotonic() - started:.1f}s.')


if __name__ == '__main__':
    main()
