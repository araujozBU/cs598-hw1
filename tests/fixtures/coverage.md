# Checkpoint 3 coverage

Run the six end-to-end tasks with:

```sh
RUN_FINAL_LIVE_TESTS=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_final_live.py -v
```

Configure QWEN_API_KEY, OPENAI_BASE_URL and MODEL for the assignment endpoint first.
Each generated task has its own 300-second limit. The new assertions reuse the
same generated tally program and do not make extra model requests. There are two
additional agent tasks: chained debugging and explicitly specified local CLI
extensions. The earlier checkpoint test files are unchanged.

| Published behavior | Checks |
| --- | --- |
| Count | 40,000 records, header-only input, shuffled records and quoted multiline fields |
| Total | Refunds, negative/zero totals, 1,001 one-cent amounts, seeded random integer-cent oracle |
| Category total | Mixed-case merging and queries, exact match rather than substring, category with spaces, missing category |
| Top | Reverse alphabetical insertion on tied totals, tie at top-1 cutoff, descending numeric order, negative totals, fewer categories than N |
| Printed category case | Lowercase names after merging case variants |
| Date range | Both endpoints, same-day range, excluded neighboring days, shuffled dates, empty range |
| CSV and CLI | Commas, escaped quotes, embedded newlines, filenames with spaces, option block permutations |
| Invocation | Direct Python subprocess with -S, separate from the harness run_python wrapper |
| Report | Exact keys, exact strings and integer types, alternate IDs and root cause |
| Causal tracing | Worker-only root-cause event, dispatch and time disambiguation, competing complete request chains, >20 gateway matches before trace evidence, payment-service-only status |
| Framework | Existing offline tests cover execution limits, API streaming, thinking disabled, call cap and completion safeguards |

Expected money values for new tally cases come from integer cents, independently
of the generated implementation. Datasets and random seeds are deterministic.
All input records in the published-requirements cases are valid; ambiguous
malformed-row behavior is not silently imposed on that task.

The JSON/invalid-row task supplies explicit LOCAL rules for JSON schemas, invalid
CSV rows, missing files and unsupported commands. These are practice cases, NOT
claims about the hidden grading contract. The harder incident adds a local
payment_status field in place of queue_depth, so reports must adapt to the actual
prompt rather than retain a fixed twelve-key schema.

The windows.py task remains generalization practice; it additionally checks
seeded arrays and invalid JSON value types. Its functional checks do not prove
O(n) complexity. Direct -S execution catches ordinary third-party import
dependencies but is not a formal dependency or security audit.

Passing these tests cannot guarantee the six unreleased requirements in either
category, all possible inputs, model reliability, or performance in the grader's
environment. Rerun live tasks to measure reliability. The read-only offline suite
does not establish that the model can solve the end-to-end tasks.
