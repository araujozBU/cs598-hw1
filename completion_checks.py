"""Completion evidence for requested deliverables and saved-program checks."""

import ast
import hashlib
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
                 money_commands=(), programming_prompt=''):
        self.workspace = workspace
        self.outputs = outputs
        self.programming = programming
        self.written = set()
        self.untested = set()
        self.failed_tests = {}
        self.passed_tests = {}
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
        self.required_options = set(re.findall(r'(?<![\w-])--[A-Za-z][\w-]*', programming_prompt))
        self.source_errors = {}

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

    def working_examples(self, path):
        """Bounded previous observations for repair context, not proof of current correctness."""
        path = str(self.workspace.file_path(path))
        return [value for key, value in self.passed_tests.items() if key[0] == path][-12:]

    def source_review(self, source, path):
        tree = ast.parse(source)
        self.source_errors.pop(path, None)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        # Only diagnose a statically declared argparse interface. Custom parsers,
        # dynamic declarations and parse_known_args may intentionally handle flags elsewhere.
        attributes = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
        declarations = [node for node in calls if isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'add_argument']
        if (declarations and 'ArgumentParser' in attributes
                and attributes.intersection({'parse_args', 'parse_intermixed_args'})
                and not attributes.intersection({'parse_known_args', 'parse_known_intermixed_args'})
                and all(node.args and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                                          for arg in node.args) for node in declarations)
                and 'REMAINDER' not in source):
            declared = {arg.value for node in declarations for arg in node.args if arg.value.startswith('--')}
            missing = sorted(self.required_options - declared)
            if missing:
                error = ('The specification requires CLI options that this argparse parser never declares: '
                            + ', '.join(missing) + '. A positional argument with nargs="*" does not accept unknown --options. '
                            'Declare every option with add_argument, choose safe dest names for Python keywords, '
                            'and read the parsed attributes in the command handlers.')
                self.source_errors[path] = error
                return {'validation_error': error,
                        'missing_options': missing,
                        'parser_source': [ast.get_source_segment(source, node) for node in declarations]}
            parsed = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                      and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                      and node.value.func.attr in ('parse_args', 'parse_intermixed_args')]
            for assignment in parsed:
                namespace = assignment.targets[0].id
                parser = ast.dump(assignment.value.func.value)
                # Passing the whole namespace to a helper, vars(), or getattr()
                # can consume options indirectly; do not guess in that case.
                if any(any(isinstance(arg, ast.Name) and arg.id == namespace for arg in node.args)
                       or any(isinstance(kw.value, ast.Name) and kw.value.id == namespace for kw in node.keywords)
                       for node in calls):
                    continue
                used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name) and node.value.id == namespace
                        and isinstance(node.ctx, ast.Load)}
                positional = [d for d in declarations if ast.dump(d.func.value) == parser
                              and not d.args[0].value.startswith('-')]
                for index, declaration in enumerate(positional):
                    keywords = {kw.arg: kw.value for kw in declaration.keywords}
                    nargs = keywords.get('nargs')
                    dest = keywords.get('dest', declaration.args[0])
                    if (isinstance(nargs, ast.Constant) and nargs.value == '*'
                            and isinstance(dest, ast.Constant) and dest.value not in used
                            and any(any(kw.arg == 'nargs' and isinstance(kw.value, ast.Constant)
                                        and kw.value.value == '?' for kw in later.keywords)
                                    for later in positional[index + 1:])):
                        error = ('Unused greedy positional consumes the following optional value: '
                                 + ast.get_source_segment(source, declaration)
                                 + '. Remove this unused add_argument declaration. Keep the later value optional '
                                 'globally and require it only in the command handler that needs it. '
                                 'Do not convert a required positional interface to a --flag.')
                        self.source_errors[path] = error
                        return {'validation_error': error,
                                'suggested_tool_call': {'name': 'replace_text', 'arguments': {
                                    'path': path, 'old': ast.get_source_segment(source, declaration),
                                    'new': 'pass  # Removed unused greedy positional.'}}}
                for declaration in positional:
                    keywords = {kw.arg: kw.value for kw in declaration.keywords}
                    nargs = keywords.get('nargs')
                    dest = keywords.get('dest', declaration.args[0])
                    if (not isinstance(nargs, ast.Constant) or nargs.value != '*'
                            or not isinstance(dest, ast.Constant) or not isinstance(dest.value, str)):
                        continue
                    scalar = [call for call in calls
                              if isinstance(call.func, ast.Name)
                              and call.func.id in ('int', 'float', 'Decimal')
                              and call.args and isinstance(call.args[0], ast.Attribute)
                              and isinstance(call.args[0].value, ast.Name)
                              and call.args[0].value.id == namespace
                              and call.args[0].attr == dest.value]
                    if scalar:
                        old = ast.get_source_segment(source, declaration)
                        new = re.sub(r'nargs\s*=\s*([\'\"])\*\1', "nargs='?'", old, count=1)
                        error = (f'{namespace}.{dest.value} is a list because its positional argument uses '
                                 'nargs="*", but a scalar conversion reads it directly. Use nargs="?" '
                                 'for one optional value and validate absence in the command handler.')
                        self.source_errors[path] = error
                        return {'validation_error': error,
                                'suggested_tool_call': {'name': 'replace_text', 'arguments': {
                                    'path': path, 'old': old, 'new': new}}}
                unused = {}
                for declaration in declarations:
                    if ast.dump(declaration.func.value) != parser:
                        continue
                    options = [arg.value for arg in declaration.args if arg.value.startswith('--')]
                    if not options:
                        continue
                    keywords = {kw.arg: kw.value for kw in declaration.keywords}
                    if 'action' in keywords and isinstance(keywords['action'], ast.Constant) and keywords['action'].value in ('help', 'version'):
                        continue
                    dest = keywords.get('dest', ast.Constant(options[0][2:].replace('-', '_')))
                    if isinstance(dest, ast.Constant) and isinstance(dest.value, str) and dest.value not in used:
                        unused.update({option: f'{namespace}.{dest.value}' for option in options if option in self.required_options})
                if unused:
                    error = ('Required options are declared but their parsed values are never read: '
                             + json.dumps(unused) + '. argparse removes declared options from positional arguments. '
                             'Update the command handlers to use these named attributes; scanning the positional list '
                             'for the option names cannot work.')
                    self.source_errors[path] = error
                    return {'validation_error': error, 'unused_options': unused}
        if not self.integer_array:
            return None
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
            self.passed_tests.pop(key, None)
            previous = self.failed_tests.get(key, {})
            signature = (result.get('stdout'), result.get('stderr'),
                         result.get('reference', {}).get('stdout'), result.get('reference', {}).get('stderr'))
            attempt_signature = (hashlib.sha256(self.workspace.file_path(arguments['path']).read_bytes()).hexdigest(),
                                 arguments.get('reference_code'), arguments.get('expected_exit_code', 0))
            result['repeat_failure'] = previous.get('attempt_signature') == attempt_signature
            self.failed_tests[key] = {
                'signature': signature,
                'attempt_signature': attempt_signature,
                'requires_reference': compared or previous.get('requires_reference', False),
                'arguments': {**arguments, 'path': str(self.workspace.file_path(arguments['path']).relative_to(self.workspace.root))}}
            self.untested.add(path)
        elif key not in self.failed_tests or not self.failed_tests[key]['requires_reference'] or compared or negative:
            self.failed_tests.pop(key, None)
            if compared and result.get('outputs_match') is True:
                self.passed_tests[key] = {'args': list(key[1]), 'stdin': key[2][:2000],
                                          'stdout': result['stdout'][:2000]}
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
        if self.source_errors:
            return 'Repair the source interface before finishing: ' + json.dumps(self.source_errors)
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
