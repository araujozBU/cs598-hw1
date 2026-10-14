"""Exact CSV checks and optional live, multi-process Checkpoint 2 conversations."""

import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Workspace


class CSVTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = Workspace(self.root)
        self.original = b'id,name,score,bonus,status\n001,"Chen, Maya",20.5,30.25,pending\n002,Alex,-1.5,0,cancelled\n'
        (self.root / 'data.csv').write_bytes(self.original)

    def tearDown(self):
        self.assertEqual((self.root / 'data.csv').read_bytes(), self.original)

    def test_read_quoted_cells_and_identifiers(self):
        result = self.workspace.execute('read_csv', {'path': 'data.csv'})
        self.assertEqual(result['rows'][0], ['001', 'Chen, Maya', '20.5', '30.25', 'pending'])

    def test_decimal_sum_average_and_row_sum(self):
        for operation, columns, extra, expected in [
            ('sum', ['score'], {}, '19.0'),
            ('average', ['score'], {}, '9.5'),
            ('row_sum', ['score', 'bonus'], {'id_column': 'id', 'row_id': '001'}, '50.75'),
        ]:
            with self.subTest(operation=operation):
                result = self.workspace.execute('calculate_csv', {
                    'path': 'data.csv', 'operation': operation, 'columns': columns, **extra})
                self.assertEqual(result['result'], expected)

    def test_update_preserves_every_other_cell(self):
        self.workspace.execute('update_csv', {'path': 'data.csv', 'output_path': 'reports/updated.csv',
                               'id_column': 'id', 'row_id': '001', 'column': 'status', 'value': 'complete'})
        expected = list(csv.reader(io.StringIO(self.original.decode())))
        expected[1][-1] = 'complete'
        with (self.root / 'reports/updated.csv').open(newline='') as f:
            self.assertEqual(list(csv.reader(f)), expected)

    def test_remove_rows_preserves_header_and_values(self):
        for operator, value, expected_rows in [('eq', 'cancelled', 1), ('ne', 'missing', 0)]:
            with self.subTest(operator=operator):
                result = self.workspace.execute('remove_csv_rows', {'path': 'data.csv',
                    'output_path': 'filtered.csv', 'column': 'status', 'operator': operator, 'value': value})
                rows = list(csv.reader(io.StringIO((self.root / 'filtered.csv').read_text())))
                self.assertEqual(rows[0], ['id', 'name', 'score', 'bonus', 'status'])
                self.assertEqual(result['row_count'], expected_rows)
                self.assertEqual(len(rows), expected_rows + 1)

    def test_copy_preserves_bom_quotes_and_line_endings(self):
        raw = b'\xef\xbb\xbfid,notes\r\n001,"first line\r\nsecond line"\r\n'
        (self.root / 'special.csv').write_bytes(raw)
        self.workspace.execute('copy_csv', {'path': 'special.csv', 'output_path': 'nested/copy.csv'})
        self.assertEqual((self.root / 'nested/copy.csv').read_bytes(), raw)

    def test_invalid_data_causes_no_output(self):
        with self.assertRaises(ValueError):
            self.workspace.execute('update_csv', {'path': 'data.csv', 'output_path': 'bad.csv',
                'id_column': 'id', 'row_id': '999', 'column': 'status', 'value': 'complete'})
        self.assertFalse((self.root / 'bad.csv').exists())
        with self.assertRaises(ValueError):
            self.workspace.execute('calculate_csv', {'path': 'data.csv', 'operation': 'sum', 'columns': ['name']})

    def test_csv_output_cannot_escape_workspace(self):
        with self.assertRaises(ValueError):
            self.workspace.execute('copy_csv', {'path': 'data.csv', 'output_path': '../escape.csv'})

    def test_in_place_update(self):
        (self.root / 'editable.csv').write_bytes(self.original)
        self.workspace.execute('update_csv', {'path': 'editable.csv', 'output_path': 'editable.csv',
            'id_column': 'id', 'row_id': '002', 'column': 'status', 'value': 'done'})
        rows = list(csv.reader(io.StringIO((self.root / 'editable.csv').read_text())))
        self.assertEqual(rows[2][-1], 'done')
        self.assertEqual(rows[1], ['001', 'Chen, Maya', '20.5', '30.25', 'pending'])


@unittest.skipUnless(os.environ.get('RUN_CHECKPOINT2_LIVE_TESTS') == '1',
                     'Set RUN_CHECKPOINT2_LIVE_TESTS=1 for live conversations')
class Checkpoint2LiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'unrelated.txt').write_bytes(b'unchanged')

    def turn(self, prompt, session):
        process = subprocess.run(['bash', str(ROOT / 'run_agent.sh'), prompt, str(session)],
                                 cwd=self.root, capture_output=True, text=True, timeout=180)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {'status', 'message'})
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result['status'], 'success')
        self.assertIsInstance(result['message'], str)
        self.assertEqual((self.root / 'unrelated.txt').read_bytes(), b'unchanged')
        print(f'Session {session}: {result["message"]}', file=sys.stderr)
        return result['message']

    def assert_number(self, answer, number):
        self.assertRegex(answer.replace(',', ''), rf'(?<![\d.]){re.escape(number)}(?:\.0+)?(?!\d|\.\d)')

    def test_total_average_copy_conversation(self):
        original = b'month,revenue\nJanuary,120\nFebruary,180\n'
        (self.root / 'sales.csv').write_bytes(original)
        self.assert_number(self.turn('Read sales.csv and tell me the total of the revenue column.', 1001), '300')
        self.assert_number(self.turn('What is the average?', 1001), '150')
        self.turn('Save a copy of the table as reports/sales_copy.csv.', 1001)
        self.assertEqual((self.root / 'reports/sales_copy.csv').read_bytes(), original)
        self.assertEqual((self.root / 'sales.csv').read_bytes(), original)

    def test_interleaved_contexts(self):
        sources = {'apples.csv': b'item,quantity\na,3\nb,9\n',
                   'revenue.csv': b'month,revenue\nJan,100\nFeb,300\n'}
        for name, content in sources.items():
            (self.root / name).write_bytes(content)
        self.assert_number(self.turn('Read apples.csv and total the quantity column.', 2001), '12')
        self.assert_number(self.turn('Read revenue.csv and total the revenue column.', 2002), '400')
        self.assert_number(self.turn('What is the average?', 2001), '6')
        self.assert_number(self.turn('What is the average?', 2002), '200')
        for name, content in sources.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_row_calculation_update_and_filter(self):
        original = b'id,interview_score,test_score,status\n1,20.5,30.25,pending\n2,10,15,cancelled\n'
        (self.root / 'candidates.csv').write_bytes(original)
        self.assert_number(self.turn('Read candidates.csv. For the row whose id is 1, add interview_score and test_score. What is the result?', 3001), '50.75')
        self.turn('In that original table, change the status for id 1 to complete. Save it as updated.csv.', 3001)
        expected = [['id', 'interview_score', 'test_score', 'status'],
                    ['1', '20.5', '30.25', 'complete'], ['2', '10', '15', 'cancelled']]
        self.assertEqual(list(csv.reader(io.StringIO((self.root / 'updated.csv').read_text()))), expected)
        self.turn('From updated.csv, remove every row whose status is cancelled and save reports/active.csv.', 3001)
        self.assertEqual(list(csv.reader(io.StringIO((self.root / 'reports/active.csv').read_text()))), expected[:2])
        self.assertEqual((self.root / 'candidates.csv').read_bytes(), original)
        self.assertEqual(list(csv.reader(io.StringIO((self.root / 'updated.csv').read_text()))), expected)


if __name__ == '__main__':
    unittest.main()
