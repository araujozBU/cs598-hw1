"""CSV tools: preserve string cells and delegate arithmetic to Decimal."""

import csv
from decimal import Decimal, InvalidOperation, localcontext
import io


def definition(name, description, properties, required):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False}}}


STRING = {'type': 'string'}
CSV_TOOLS = [
    definition('read_csv', 'Read CSV header, row count, and a bounded preview (up to 100 rows). '
               'For large-table calculations use calculate_csv or analyze_python, not the preview.',
               {'path': STRING}, ['path']),
    definition('calculate_csv',
               'Calculate a column sum or average, or add specified columns in one row identified by its ID. '
               'For sum/average provide one column. For row_sum provide columns, id_column, and row_id. '
               'Use this tool for CSV arithmetic, rather than calculating yourself.',
               {'path': STRING, 'operation': {'type': 'string', 'enum': ['sum', 'average', 'row_sum']},
                'columns': {'type': 'array', 'items': STRING, 'minItems': 1},
                'id_column': STRING, 'row_id': STRING}, ['path', 'operation', 'columns']),
    definition('update_csv', 'Update exactly one cell in a row identified by an ID and save a CSV. '
               'Use the original path as output_path only when the user requests an in-place update.',
               {'path': STRING, 'output_path': STRING, 'id_column': STRING, 'row_id': STRING,
                'column': STRING, 'value': STRING},
               ['path', 'output_path', 'id_column', 'row_id', 'column', 'value']),
    definition('remove_csv_rows', 'Remove rows matching a condition and save the remaining table. '
               'eq/ne compare exact strings; lt/le/gt/ge compare numeric values. Preserve the header.',
               {'path': STRING, 'output_path': STRING, 'column': STRING,
                'operator': {'type': 'string', 'enum': ['eq', 'ne', 'lt', 'le', 'gt', 'ge']},
                'value': STRING}, ['path', 'output_path', 'column', 'operator', 'value']),
    definition('copy_csv', 'Save an exact copy of a CSV table, creating output parent directories.',
               {'path': STRING, 'output_path': STRING}, ['path', 'output_path']),
]
CSV_NAMES = {item['function']['name'] for item in CSV_TOOLS}


def read_table(workspace, path):
    raw = workspace.file_path(path).read_bytes()
    reader = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    try:
        records = list(reader)
    except csv.Error as exc:
        raise ValueError(f'Invalid CSV: {exc}') from None
    if not records or not records[0] or any(not h for h in records[0]):
        raise ValueError('CSV must have a nonempty header with named columns.')
    headers, rows = records[0], records[1:]
    if len(set(headers)) != len(headers):
        raise ValueError('CSV column names must be unique.')
    if any(len(row) != len(headers) for row in rows):
        raise ValueError('Every CSV row must have the same number of cells as the header.')
    return raw, headers, rows


def number(value):
    try:
        result = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f'Not a numeric cell: {value!r}') from None
    if not result.is_finite():
        raise ValueError('Numeric values must be finite.')
    return result


def column_index(headers, name):
    if name not in headers:
        raise ValueError(f'CSV column does not exist: {name}')
    return headers.index(name)


def select_row(headers, rows, id_column, row_id):
    index = column_index(headers, id_column)
    matches = [row for row in rows if row[index] == row_id]
    if len(matches) != 1:
        raise ValueError(f'Expected one row with {id_column}={row_id!r}; found {len(matches)}.')
    return matches[0]


def write_table(workspace, output_path, headers, rows):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(headers)
    writer.writerows(rows)
    workspace.write_bytes(output_path, buffer.getvalue().encode('utf-8'))
    return {'written': output_path, 'row_count': len(rows)}


def execute_csv(workspace, name, arguments):
    schema = next(item['function']['parameters'] for item in CSV_TOOLS
                  if item['function']['name'] == name)
    if (not isinstance(arguments, dict) or not set(schema['required']).issubset(arguments)
            or not set(arguments).issubset(schema['properties'])):
        raise ValueError('Invalid CSV tool arguments.')
    if any(not isinstance(value, str) for key, value in arguments.items() if key != 'columns'):
        raise ValueError('CSV arguments other than columns must be strings.')
    raw, headers, rows = read_table(workspace, arguments['path'])
    if name == 'read_csv':
        preview, size = [], 0
        for row in rows[:100]:
            size += sum(len(cell) for cell in row)
            if size > 24000:
                break
            preview.append(row)
        return {'columns': headers, 'rows': preview, 'row_count': len(rows),
                'truncated': len(preview) < len(rows)}
    if name == 'copy_csv':
        workspace.write_bytes(arguments['output_path'], raw)
        return {'written': arguments['output_path'], 'row_count': len(rows)}
    if name == 'calculate_csv':
        columns = arguments['columns']
        if not isinstance(columns, list) or not columns or any(not isinstance(c, str) for c in columns):
            raise ValueError('columns must be a nonempty list of column names.')
        if len(set(columns)) != len(columns):
            raise ValueError('Do not repeat a column in a calculation.')
        indices = [column_index(headers, c) for c in columns]
        operation = arguments['operation']
        if operation == 'row_sum':
            if 'id_column' not in arguments or 'row_id' not in arguments:
                raise ValueError('row_sum requires id_column and row_id.')
            row = select_row(headers, rows, arguments['id_column'], arguments['row_id'])
            values = [number(row[index]) for index in indices]
        elif operation in ('sum', 'average'):
            if len(indices) != 1 or 'id_column' in arguments or 'row_id' in arguments:
                raise ValueError('Column sum/average requires exactly one column and no row selector.')
            values = [number(row[indices[0]]) for row in rows]
            if operation == 'average' and not values:
                raise ValueError('Cannot average an empty column.')
        else:
            raise ValueError('Unknown calculation operation.')
        with localcontext() as context:
            context.prec = 50
            result = sum(values, Decimal(0))
            if operation == 'average':
                result /= len(values)
            result_text = format(result, 'f')
        return {'operation': operation, 'columns': columns, 'result': result_text, 'value_count': len(values)}
    if name == 'update_csv':
        row = select_row(headers, rows, arguments['id_column'], arguments['row_id'])
        row[column_index(headers, arguments['column'])] = arguments['value']
        return write_table(workspace, arguments['output_path'], headers, rows)
    if name == 'remove_csv_rows':
        index = column_index(headers, arguments['column'])
        operator, target = arguments['operator'], arguments['value']
        if operator not in ('eq', 'ne', 'lt', 'le', 'gt', 'ge'):
            raise ValueError('Unknown comparison operator.')
        def matches(row):
            value = row[index]
            if operator == 'eq':
                return value == target
            if operator == 'ne':
                return value != target
            left, right = number(value), number(target)
            return {'lt': left < right, 'le': left <= right,
                    'gt': left > right, 'ge': left >= right}[operator]
        kept = [row for row in rows if not matches(row)]
        return {**write_table(workspace, arguments['output_path'], headers, kept),
                'removed': len(rows) - len(kept)}
    raise ValueError('Unknown CSV tool.')
