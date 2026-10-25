"""Independent integer-cent oracles for published tally behavior (no hidden guesses)."""
import csv
import itertools
import random


def money(cents):
    return ('-' if cents < 0 else '') + f'{abs(cents) // 100}.{abs(cents) % 100:02d}'


def write_rows(root, filename, rows):
    with (root / filename).open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['date', 'description', 'category', 'amount'])
        writer.writerows((date, description, category, money(cents))
                         for date, description, category, cents in rows)


def expected_cases(rows):
    groups = {}
    for date, description, category, cents in rows:
        key = category.lower()
        groups[key] = groups.get(key, 0) + cents
    cases = [(['count'], f'{len(rows)}\n'),
             (['total'], money(sum(row[3] for row in rows)) + '\n')]
    for category in ['fOoD', 'FOOD DELIVERY', 'missing', *groups]:
        cases.append((['total', '--category', category], money(groups.get(category.lower(), 0)) + '\n'))
    ordered = sorted(groups, key=lambda name: (-groups[name], name))
    for n in (1, 2, len(groups) + 3):
        cases.append((['top', str(n)], ''.join(f'{name}\t{money(groups[name])}\n' for name in ordered[:n])))
    for lo, hi in [('2026-01-01', '2026-01-03'), ('2026-01-02', '2026-01-02'),
                   ('2025-12-31', '2026-01-01'), ('2026-01-03', '2026-02-01'),
                   ('2027-01-01', '2027-01-02')]:
        selected = [row for row in rows if lo <= row[0] <= hi]
        cases.append((['range', '--from', lo, '--to', hi],
                      f'count={len(selected)} total={money(sum(row[3] for row in selected))}\n'))
    return cases


def tally_datasets():
    # Reverse alphabetical insertion, split case variants, tied totals and a
    # smaller early category distinguish sum-sort, case merge and tie-break bugs.
    adversarial = [
        ('2026-01-03', 'Last boundary', 'Zebra', 500),
        ('2026-01-01', 'First boundary', 'apple', 200),
        ('2026-01-02', 'Middle', 'APPLE', 300),
        ('2026-01-02', 'He said "hello", then\nleft', 'Food Delivery', 101),
        ('2025-12-31', 'Before', 'food', -20),
        ('2026-01-04', 'After', 'FOOD', 10),
        ('2026-01-02', 'Refund', 'negative', -1000),
    ]
    rng = random.Random(598)
    random_rows = [(rng.choice(['2025-12-31', '2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04']),
                    f'item {i}, "quoted"', rng.choice(['Food', 'FOOD', 'food', 'Travel', 'BOOKS', 'Food Delivery']),
                    rng.randint(-99999, 99999)) for i in range(503)]
    return [('reverse ties.csv', adversarial), ('shuffled.csv', random_rows),
            ('reversed.csv', list(reversed(random_rows))),
            ('cancellation.csv', [('2026-01-02', 'Credit', 'Food', 29),
                                  ('2026-01-02', 'Debit', 'FOOD', -29)]),
            ('cents.csv', [('2026-01-02', 'Cent', 'Food', 1)] * 1001),
            ('header only.csv', [])]


def assert_tally_coverage(test, root, run):
    for filename, rows in tally_datasets():
        write_rows(root, filename, rows)
        for args, expected in expected_cases(rows):
            for position in (1, len(args)):
                invocation = args[:position] + ['--file', filename] + args[position:]
                with test.subTest(file=filename, args=invocation):
                    test.assertEqual(run('tally.py', invocation), expected)
    filename, rows = tally_datasets()[0]
    # Permute complete option/value blocks, never split an option from its value.
    for blocks in itertools.permutations([['--file', filename], ['--from', '2026-01-01'], ['--to', '2026-01-03']]):
        args = ['range', *[item for block in blocks for item in block]]
        with test.subTest(args=args):
            test.assertEqual(run('tally.py', args), 'count=5 total=1.01\n')


LOCAL_EXTENSIONS = '''
Additional LOCAL PRACTICE requirements, not the unreleased grading requirements:
- For this task, a valid row has exactly four fields, a real YYYY-MM-DD date,
  a nonempty category, and a finite amount with at most two decimal places.
  Skip invalid rows for all commands. Do not count the header.
- --json may appear anywhere after the command. For count, print {"count":K};
  for total (including --category), print {"total":"X.XX"}; for range, print
  {"count":K,"total":"X.XX"}; for top, print a JSON array of objects with
  string fields category and total, in the same order as text output.
  JSON whitespace and key order do not matter. Print no other stdout.
- A missing input file or unsupported command must exit nonzero with a useful
  message on stderr and no stdout.
'''
