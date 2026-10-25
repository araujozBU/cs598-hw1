"""Create synthetic final-checkpoint input files in an empty task workspace."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from final_fixtures import ALTERNATIVE_PROGRAM, debugging_fixture, programming_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['programming', 'debugging', 'debugging-alternate', 'programming-alternate'])
    parser.add_argument('workspace', type=Path)
    args = parser.parse_args()
    root = args.workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error('Use a fresh empty workspace; existing files will not be overwritten.')
    expected = None
    if args.kind == 'programming':
        prompt = programming_fixture(root)
    elif args.kind == 'programming-alternate':
        prompt = ALTERNATIVE_PROGRAM
    else:
        prompt, expected = debugging_fixture(root, alternate=args.kind == 'debugging-alternate')
    (root / 'TASK.md').write_text(prompt)
    (root / 'unrelated/empty').mkdir(parents=True)
    (root / 'unrelated/sentinel.bin').write_bytes(b'\x00\xffunchanged')
    print('Prepared synthetic inputs in: ' + str(root))
    if expected is not None:
        print('Expected report (for your review; not saved in the task workspace):')
        print(json.dumps(expected, indent=2))
    elif args.kind == 'programming':
        print('Expected: count 40000; total 250000.00; food total 100000.00.')
    else:
        print('Expected for K=2 and input [1,-2,3,4]: [-1,1,7].')


if __name__ == '__main__':
    main()
