"""Bounded inspection and Python execution tools for programming/debugging tasks."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from csv_tools import definition
from sessions import SESSION_DIRECTORY


STRING = {'type': 'string'}
INTEGER = {'type': 'integer', 'minimum': 1}
WORKSPACE_TOOLS = [
    definition('replace_text', 'Edit an existing UTF-8 source file by replacing one exact text snippet. '
               'The old snippet must occur exactly once unless replace_all=true (use that for an intentional global identifier rename). '
               'Prefer this for a small bug fix instead of rewriting the whole program.',
               {'path': STRING, 'old': STRING, 'new': STRING, 'replace_all': {'type': 'boolean'}}, ['path', 'old', 'new']),
    definition('list_files', 'List files/directories in a workspace directory (not recursive).',
               {'path': STRING}, []),
    definition('read_lines', 'Read a bounded range of numbered lines. Use for large files, source code, and TASK.md.',
               {'path': STRING, 'start_line': INTEGER, 'max_lines': INTEGER}, ['path']),
    definition('search_file', 'Search a file for an exact literal substring, returning numbered matching lines '
               'and nearby context (up to 20 matches). Inspect ALL returned matches; an earlier success may be unrelated to a later failure. '
               'If truncated, continue at next_line. Follow IDs across large logs with successive searches.',
               {'path': STRING, 'query': STRING, 'context_lines': {'type': 'integer', 'minimum': 0},
                'start_line': INTEGER}, ['path', 'query']),
    definition('run_python', 'Test a saved Python program directly. Example: {"path":"program.py","args":["count"]}. '
               'Do NOT use subprocess or paste a replacement implementation to test it. Execution is read-only: no file writes, network, '
               'subprocesses, or access to session history. stdout, stderr, exit code and timeout are returned. '
               'Use write_file to create/fix the program before testing it. Only standard library is available. '
               'For exact-output checks supply reference_code, an independent Python calculation reading actual inputs and printing the answer. Do not import/run the candidate program from the reference. The tool returns both outputs and outputs_match. '
               'For invalid-input tests, set expected_exit_code to the nonzero exit code the program should return and omit reference_code.',
               {'path': STRING, 'args': {'type': 'array', 'items': STRING},
                'stdin': STRING, 'reference_code': STRING, 'expected_exit_code': {'type': 'integer'},
                'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ['path']),
    definition('analyze_python', 'Run a short read-only Python snippet to inspect data, calculate independent expected results, '
               'or validate a report. No subprocess/network/writes. To test a saved program use run_python with its path.',
               {'code': STRING, 'stdin': STRING,
                'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ['code']),
]
WORKSPACE_NAMES = {t['function']['name'] for t in WORKSPACE_TOOLS}
TEXT_LIMIT = 24000


def integer(arguments, name, default, low, high):
    value = arguments.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}.')
    return value


def execute_workspace(workspace, name, arguments):
    schema = next(t['function']['parameters'] for t in WORKSPACE_TOOLS if t['function']['name'] == name)
    if (not isinstance(arguments, dict) or not set(schema['required']).issubset(arguments)
            or not set(arguments).issubset(schema['properties'])):
        raise ValueError('Invalid workspace tool arguments.')
    if name in ('run_python', 'analyze_python'):
        return run_python(workspace, arguments)
    if name == 'replace_text':
        old, new = arguments['old'], arguments['new']
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError('old must be nonempty text and new must be text.')
        if old == new:
            raise ValueError('old and new are identical; this would not repair anything. Choose a real change or verify the test expectation.')
        content = workspace.file_path(arguments['path']).read_bytes().decode('utf-8')
        replace_all = arguments.get('replace_all', False)
        if type(replace_all) is not bool:
            raise ValueError('replace_all must be a boolean.')
        count = content.count(old)
        if not count or (count != 1 and not replace_all):
            raise ValueError(f'old matched {count} locations. Use a unique snippet, or replace_all=true for an intentional global replacement.')
        workspace.write_bytes(arguments['path'], content.replace(old, new, -1 if replace_all else 1).encode('utf-8'))
        return {'written': arguments['path'], 'replacements': count if replace_all else 1}
    if name == 'list_files':
        directory = workspace.root if arguments.get('path', '.') == '.' else workspace.file_path(arguments['path'])
        entries = []
        for child in sorted(directory.iterdir()):
            if child.name == SESSION_DIRECTORY or child.is_symlink():
                continue
            entries.append({'name': child.name, 'type': 'directory' if child.is_dir() else 'file',
                            'bytes': child.stat().st_size if child.is_file() else None})
            if len(entries) == 500:
                break
        return {'entries': entries, 'limit': 500}
    path = workspace.file_path(arguments['path'])
    start = integer(arguments, 'start_line', 1, 1, 100000000)
    if name == 'read_lines':
        count = integer(arguments, 'max_lines', 100, 1, 500)
        lines, size, more, next_line = [], 0, False, None
        with path.open(encoding='utf-8', errors='replace') as handle:
            for number, line in enumerate(handle, 1):
                if number < start:
                    continue
                if len(lines) >= count or size >= TEXT_LIMIT:
                    more, next_line = True, number
                    break
                content = line.rstrip('\r\n')[:TEXT_LIMIT - size]
                lines.append({'line': number, 'text': content, 'line_truncated': len(line.rstrip('\r\n')) > len(content)})
                size += len(content)
        return {'lines': lines, 'has_more': more, 'next_line': next_line}
    query = arguments['query']
    if not isinstance(query, str) or not query:
        raise ValueError('query must be a nonempty literal string.')
    context = integer(arguments, 'context_lines', 2, 0, 10)
    maximum = 20
    # Reading locally is cheap; only bounded search results enter model context.
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    matches, selected, size, truncated, next_line = [], {}, 0, False, None
    for index in range(start - 1, len(lines)):
        if query not in lines[index]:
            continue
        if len(matches) >= maximum or size >= TEXT_LIMIT:
            truncated, next_line = True, index + 1
            break
        matches.append(index + 1)
        for nearby in range(max(0, index - context), min(len(lines), index + context + 1)):
            if nearby + 1 in selected:
                continue
            if size >= TEXT_LIMIT:
                truncated, next_line = True, index + 1
                break
            selected[nearby + 1] = lines[nearby][:TEXT_LIMIT - size]
            size += len(selected[nearby + 1])
    return {'matched_lines': matches, 'lines': [{'line': n, 'text': s} for n, s in sorted(selected.items())],
            'truncated': truncated, 'next_line': next_line}


def run_python(workspace, arguments):
    if ('path' in arguments) == ('code' in arguments):
        raise ValueError('Provide exactly one of path or code.')
    args = arguments.get('args', [])
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError('args must be a list of strings.')
    for field in ('code', 'stdin', 'reference_code'):
        if field in arguments and not isinstance(arguments[field], str):
            raise ValueError(f'{field} must be a string.')
    duration = integer(arguments, 'timeout_seconds', 15, 1, 30)
    if type(arguments.get('expected_exit_code', 0)) is not int:
        raise ValueError('expected_exit_code must be an integer.')
    payload = {**arguments, 'root': str(workspace.root)}
    if 'path' in arguments:
        payload['path'] = str(workspace.file_path(arguments['path']))
    runner = Path(__file__).with_name('python_runner.py')
    # Capture to temporary files, not unbounded memory, and never inherit secrets.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(runner)],
                                   cwd=workspace.root, env={'PYTHONIOENCODING': 'utf-8'},
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        timed_out = False
        try:
            try:
                process.communicate(json.dumps(payload).encode('utf-8'), timeout=duration)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.communicate()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        stdout.seek(0)
        stderr.seek(0)
        out, err = stdout.read(TEXT_LIMIT + 1), stderr.read(TEXT_LIMIT + 1)
        result = {'exit_code': process.returncode, 'timed_out': timed_out,
                'stdout': out[:TEXT_LIMIT].decode('utf-8', errors='replace'),
                'stderr': err[:TEXT_LIMIT].decode('utf-8', errors='replace'),
                'output_truncated': len(out) > TEXT_LIMIT or len(err) > TEXT_LIMIT}

    if 'reference_code' in arguments:
        reference = run_python(workspace, {'code': arguments['reference_code'], 'args': args,
            'stdin': arguments.get('stdin', ''), 'timeout_seconds': duration})
        result['reference'] = reference
        result['outputs_match'] = (result['exit_code'] == reference['exit_code'] == arguments.get('expected_exit_code', 0)
            and not result['output_truncated'] and not reference['output_truncated']
            and result['stdout'] == reference['stdout'])
        result['hint'] = 'Compare both outputs and review the reference calculation before changing source. Reference code is a check, not a grader.'
        if (reference['exit_code'] != arguments.get('expected_exit_code', 0)
                or reference['timed_out'] or reference['output_truncated']):
            result['hint'] = ('The reference calculation failed. Repair reference_code and rerun this same check. '
                              'The saved program output is preserved; do not rewrite the program to match a broken reference.')
    return result
