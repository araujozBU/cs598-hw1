"""Completion evidence for requested deliverables and saved-program checks."""

import ast
import json
import re


def requested_outputs(prompt, programming=False):
    """Recognize explicit output declarations, without treating input paths as outputs.

    These are syntax cues only; they contain no assignment-specific filenames,
    solutions, schemas, or expected answers.
    """
    filename = r'[`\"\']?([\w./-]+\.[A-Za-z0-9]+)[`\"\']?'
    patterns = [
        r'\b(?:final\s+)?deliverable\s+(?:is|must be)\s+(?:a\s+)?(?:file\s+)?(?:named\s+)?' + filename,
        r'\b(?:write|save|create|produce)\s+' + filename,
    ]
    if programming:
        patterns.append(r'\b(?:implement|write|create)\s+(?:(?:a|an)\s+)?'
                        r'(?:(?:Python\s*3?|command-line|tool|program|script)\s+)*' + filename)
    outputs = set()
    for pattern in patterns:
        outputs.update(match.group(1).rstrip('.') for match in re.finditer(pattern, prompt, re.I))
    return outputs


def scalar_money_commands(prompt):
    if not re.search(r'\bmoney\b.{0,120}\b(?:two|2)\s+decimal', prompt, re.I | re.S):
        return set()
    return set(re.findall(r'python(?:3)?\s+\S+\.py\s+([\w-]+)[^`\n]*`?\s+prints\s+(?:the\s+)?(?:sum|total)\b',
                          prompt, re.I))


class CompletionChecks:
    def __init__(self, workspace, outputs, programming, report_prompt='', validate_json=False, integer_array=False,
                 money_commands=()):
        self.workspace = workspace
        self.outputs = outputs
        self.programming = programming
        self.written = set()
        self.untested = set()
        self.failed_tests = {}
        self.report_keys = set(re.findall(r'^\s*\d+\.\s*(?:\*\*)?[`\"]?([A-Za-z_]\w*)[`\"]?\s*(?:\*\*)?\s*[:—–-]',
                                          report_prompt, re.M))
        self.exact_evidence = bool(re.search(r'\b(?:copy values exactly|exactly (?:from|as))\b', report_prompt, re.I))
        self.evidence = set()
        self.validate_json = validate_json or bool(report_prompt)
        self.integer_array = integer_array
        self.integer_input_checks = {'boolean', 'float', 'string'} if integer_array else set()
        self.valid_integer_args = {}
        self.money_commands = set(money_commands)
        self.money_zero_pending = set(money_commands)

    def output_error(self, arguments, result):
        command = next((arg for arg in arguments.get('args', []) if arg in self.money_commands), None)
        output = result['stdout']
        if (command and result['exit_code'] == 0 and arguments.get('expected_exit_code', 0) == 0
                and re.fullmatch(r'-?\d+(?:\.\d+)?\n?', output)
                and not re.fullmatch(r'-?\d+\.\d{2}\n?', output)):
            return (f'The prompt requires two decimals for money. {command} printed {output!r}. '
                    'Format the numeric result explicitly, including zero, using fixed-point formatting such as f"{value:.2f}".')
        return None

    def suggested_check(self):
        if not self.integer_input_checks or not self.valid_integer_args:
            return None
        (path, args), values = next(iter(self.valid_integer_args.items()))
        values = list(values) or [0]
        kind = sorted(self.integer_input_checks)[0]
        values[0] = {'boolean': True, 'float': 1.5, 'string': '1'}[kind]
        return {'name': 'run_python', 'arguments': {
            'path': str(self.workspace.file_path(path).relative_to(self.workspace.root)),
            'args': list(args), 'stdin': json.dumps(values), 'expected_exit_code': 1}}

    def pending_checks(self):
        return [{'arguments': item['arguments'], 'reference_required': item['requires_reference']}
                for item in self.failed_tests.values()]

    def source_review(self, source, path):
        if not self.integer_array:
            return None
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'isinstance' and len(node.args) == 2]
        if any(isinstance(node.args[1], ast.Name) and node.args[1].id == 'bool' for node in calls):
            return None  # An explicit boolean guard may make an integer check safe.
        for node in calls:
            if isinstance(node.args[1], ast.Name) and node.args[1].id == 'int':
                old = ast.get_source_segment(source, node)
                value = ast.get_source_segment(source, node.args[0])
                return {'validation_error': 'This specification requires strict JSON integers, but '
                            + old + ' also accepts booleans. Use an exact type check.',
                        'suggested_tool_call': {'name': 'replace_text', 'arguments': {
                            'path': path, 'old': old, 'new': f'(type({value}) is int)',
                            'replace_all': True}}}
        return None

    def observe(self, result):
        if isinstance(result.get('content'), str):
            self.evidence.add(result['content'])
        for line in result.get('lines', []):
            if isinstance(line.get('text'), str):
                self.evidence.add(line['text'])

    def changed(self, path):
        path = str(self.workspace.file_path(path))
        self.written.add(path)
        if self.programming and path.endswith('.py'):
            self.untested.add(path)
            self.money_zero_pending = set(self.money_commands)
            if self.integer_array:
                self.integer_input_checks = {'boolean', 'float', 'string'}
                self.valid_integer_args.clear()

    def tested(self, arguments, result, failed):
        path = str(self.workspace.file_path(arguments['path']))
        key = (path, tuple(arguments.get('args', [])), arguments.get('stdin', ''))
        negative = arguments.get('expected_exit_code', 0) != 0
        compared = 'reference_code' in arguments and not negative
        if failed:
            # Keep a failed comparison pending even if a later invocation only
            # checks the exit code or a different command happens to work.
            previous = self.failed_tests.get(key, {})
            self.failed_tests[key] = {
                'requires_reference': compared or previous.get('requires_reference', False),
                'arguments': {**arguments, 'path': str(self.workspace.file_path(arguments['path']).relative_to(self.workspace.root))}}
            self.untested.add(path)
        elif key not in self.failed_tests or not self.failed_tests[key]['requires_reference'] or compared or negative:
            self.failed_tests.pop(key, None)
            self.untested.discard(path)
            if arguments.get('expected_exit_code', 0) == 0 and result['stdout'].strip() == '0.00':
                self.money_zero_pending.difference_update(arguments.get('args', []))
            if self.integer_array:
                try:
                    values = json.loads(arguments.get('stdin', ''))
                    if isinstance(values, list) and arguments.get('expected_exit_code', 0) == 0 and all(type(value) is int for value in values):
                        self.valid_integer_args[key[:2]] = values
                    elif isinstance(values, list) and arguments.get('expected_exit_code', 0) != 0 and key[:2] in self.valid_integer_args:
                        for kind, cls in [('boolean', bool), ('float', float), ('string', str)]:
                            if any(type(value) is cls for value in values) and all(type(value) in (int, cls) for value in values):
                                self.integer_input_checks.discard(kind)
                except ValueError:
                    pass

    def error(self):
        for name in sorted(self.outputs):
            path = self.workspace.file_path(name)
            if not path.is_file():
                return f'The requested deliverable {name} does not exist. Write it before finishing.'
            if str(path) not in self.written:
                return f'Write or update the requested deliverable {name} before finishing.'
            if self.validate_json and path.suffix.lower() == '.json':
                try:
                    report = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeError):
                    return f'The requested deliverable {name} is not valid JSON. Repair it before finishing.'
                if self.report_keys:
                    if not isinstance(report, dict) or set(report) != self.report_keys:
                        return f'{name} must contain exactly the requested report keys: ' + ', '.join(sorted(self.report_keys))
                    for key, value in report.items():
                        if value is None or isinstance(value, str) and not value.strip():
                            return f'{name}: required field {key} is empty. Follow the linked identifiers to find its log evidence.'
                        if self.exact_evidence and isinstance(value, str) and not any(value in line for line in self.evidence):
                            return f'{name}: field {key} has no inspected log evidence. Read/search its source and copy the value exactly.'
        if self.programming and not any(path.endswith('.py') for path in self.written):
            return 'Implement and test the requested Python source before reporting success; inspection alone is insufficient.'
        if self.failed_tests:
            return ('Rerun and resolve every failed saved-program check before finishing. '
                    'Use the exact arguments AND stdin below. For a failed reference comparison, correct the program '
                    'or reference and rerun with reference_code. Pending checks: '
                    + json.dumps(self.pending_checks(), ensure_ascii=False))
        if self.untested:
            return 'Test the saved program with run_python after the latest edit: ' + ', '.join(sorted(self.untested))
        if self.money_zero_pending:
            return ('The task requires scalar money totals with exactly two decimals. After the latest source edit, '
                    'test an actual empty/no-match selection for these commands and verify stdout is 0.00 followed by a newline: '
                    + ', '.join(sorted(self.money_zero_pending))
                    + '. Decimal zero printed without explicit formatting becomes 0, which violates the output contract. '
                    'Use a real empty selection; do not hard-code a zero result or guess data totals.')
        if self.integer_input_checks:
            return ('The specification requires a strict JSON integer array. Use run_python with a valid CLI argument '
                    'and first verify a valid integer array succeeds. With those SAME arguments, test SEPARATE arrays '
                    'containing only integers plus ONE invalid type at a time, expecting a nonzero exit code: '
                    + ', '.join(sorted(self.integer_input_checks))
                    + '. Python bool is a subclass of int; validate type(value) is int rather than coercing values.'
                    + (' Next required check: ' + json.dumps(self.suggested_check()) if self.suggested_check() else ''))
        return None
