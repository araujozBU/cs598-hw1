# CS598 Homework 1 — File and CSV agent

This agent implements its own Qwen tool-calling loop using only Python's standard
library. No agent SDK or additional Python packages are required.

## Requirements and files

- Python 3.10 or later and Bash on Linux or macOS.
- `run_agent.sh`: entry point; resolves source code relative to itself and preserves the task working directory.
- `agent.py`: model requests, tool definitions, workspace operations, and final JSON output.
- `sessions.py`: persistent conversation history and locking, isolated by workspace and session ID.
- `csv_tools.py`: CSV reading, decimal calculations, updates, filtering, and copying.
- `scripts/test_connection.py`: one small endpoint connectivity check.
- `tests/test_agent.py`: generated test inputs, expected results, offline checks, and optional live tests.
- `tests/test_sessions.py`: separate-process persistence and session isolation checks with a scripted model.
- `tests/test_csv.py`: CSV checks and optional live Checkpoint 2 conversations.
- `walkthrough.md`: a step-by-step explanation of the harness and an example request.

Development environment: macOS with Python 3.13.7. Linux has not yet been tested.

## Run

Set `QWEN_API_KEY` in your environment. Do not put credentials in source files.
`OPENAI_BASE_URL` defaults to `https://llm.montii.me/v1` and `MODEL` defaults
to `Qwen/Qwen3-14B`. The agent requires that model name.

From your task workspace, invoke the submission's launcher by absolute path:

```bash
bash /absolute/path/to/submission/run_agent.sh 'Write "hello" to notes/greeting.txt.'
```

The first argument is the complete prompt. Standard output contains exactly one
JSON object with `status` and `message`. Exit status is zero for success and
nonzero for failure. Read-and-answer results appear in `message`.

## Checkpoint 2: persistent sessions and CSV operations

Pass the numeric session ID as the second argument:

```bash
bash /absolute/path/to/submission/run_agent.sh 'Read sales.csv and total revenue.' '1001'
```

Use the same workspace and ID for follow-up turns. Conversation history persists
across processes in `.agent_sessions/<id>.json` inside that workspace. Different
IDs have separate histories, and the same ID in another workspace starts a fresh
conversation. The hidden directory is reserved for the harness; model file tools
cannot access it. A per-session lock prevents concurrent turns from overwriting
each other's histories, and history saves use an atomic replacement.

Calls without an ID retain Checkpoint 1 behavior: a fresh conversation and no
session storage. Each call still emits exactly one JSON result within its own
170-second limit. Corrupt history produces an error instead of silently losing
the conversation. Keep generated session directories out of submissions.

CSV tools use Python's `csv` module and `Decimal` arithmetic. Supported operations
are reading values, column sums/averages, adding selected numeric cells in an
ID-selected row, updating one cell, removing rows by a condition, and copying a
table. Source files remain unchanged when a different output path is requested.
Cell values and row/column order are preserved except for requested changes;
edited CSV output uses LF line endings and standard CSV quoting. Copies preserve
the original bytes. Arithmetic expects finite decimal values, not currency strings
or blank numeric cells; ambiguous IDs, missing columns, and malformed tables
produce tool errors rather than silently changing or skipping data.

### Manual three-turn test, without a helper function

Start in the submission folder with Bash and the API configuration set as in the
manual setup below. Keep the same task directory for all three turns:

```bash
submission_dir="$(pwd -P)"
task_dir="$(mktemp -d)"
cd "$task_dir"
printf 'month,revenue\nJanuary,120\nFebruary,180\n' > sales.csv

bash "$submission_dir/run_agent.sh" 'Read sales.csv and tell me the total of the revenue column.' '1001'
echo "Exit code: $?"

bash "$submission_dir/run_agent.sh" 'What is the average?' '1001'
echo "Exit code: $?"

bash "$submission_dir/run_agent.sh" 'Save a copy of the table as reports/sales_copy.csv.' '1001'
echo "Exit code: $?"

cat reports/sales_copy.csv
cat sales.csv
```

Expected answers: total **300**, average **150**, then a copy with the original
header and two rows. Both files should contain the same table. Each exit code
should be 0. To test isolation, establish another file and column with a different
session ID, then ask each ID "What is the average?" without repeating the file.

### Checkpoint 2 tests

Run offline checks from the submission folder:

```bash
python3 -m unittest discover -s tests -v
```

There are 27 offline tests and 13 optional live tests. To run only the three live
Checkpoint 2 conversations (10 separate agent invocations, sequentially):

```bash
RUN_CHECKPOINT2_LIVE_TESTS=1 python3 tests/test_csv.py Checkpoint2LiveTests -v
```

These cover sum/average/copy, interleaved sessions, and row arithmetic/update/filter.
The Checkpoint 2 submission archive is `hw1-2.zip`. It includes the complete
updated framework, documentation, and tests. Generated session histories,
credentials, caches, and temporary workspaces are excluded. The older `hw1-1.zip`
is the previous checkpoint's snapshot and does not include these updates.
Checkpoint 2 uses `submit hw1-2`, due October 11, 2026, at 11:59 p.m. Eastern.

## How it works

The harness sends the prompt and tool definitions to `/chat/completions`. Qwen
requests `read_file`, `write_file`, or `delete_file`; Python validates and executes
each operation and returns its result to Qwen. The model calls `finish` with the
final status and answer. The harness also accepts a strictly validated JSON final
result if the endpoint renders completion as text. Without a session ID, success
requires prior successful tool execution in that call. Session turns may answer
from remembered context or acknowledge an instruction without a new file operation.

Paths must stay within the task workspace. Symlinks are rejected, writes use
UTF-8 without automatic newlines, missing parent directories are created, and
deletion never removes directories. The harness uses a 170-second overall timer
and at most 16 model turns. It disables thinking as in the course quick start.
Each response is capped at 4,096 output tokens; oversized responses fail explicitly.
Set `AGENT_DEBUG=1` to emit model completion text, tool names, and tool errors to
stderr for troubleshooting; this can include task data.

## Tests

### Quick automated check

After setting your API key as shown in the manual walkthrough below, run all
local and live tests from the submission folder:

```bash
RUN_LIVE_TESTS=1 RUN_EXTENDED_LIVE_TESTS=1 RUN_CHECKPOINT2_LIVE_TESTS=1 python3 -m unittest discover -s tests -v
```

Expect `Ran 40 tests` and `OK`, with no skipped tests when all three flags are set.
Each live case invokes the Bash launcher from a fresh workspace, as the grader
does. These are our own cases, not the hidden grading tests. Review the summaries
printed by the tests as well as the automated result.

### Individual test groups

From the submission folder:

```bash
python3 -m unittest discover -s tests -v
```

Offline tests cover exact bytes, empty files, preserved inputs and neighboring
files, path boundaries, symlinks, the model/tool loop, and launch from another
working directory. No network requests are made by default.

With `QWEN_API_KEY` set, run the four live cases sequentially:

```bash
RUN_LIVE_TESTS=1 python3 -m unittest discover -s tests -v
```

For six additional cases (empty file, Unicode and spaces, nested deletion,
multiple requested facts, missing source, and a 40,000-byte summary input):

```bash
RUN_EXTENDED_LIVE_TESTS=1 python3 tests/test_agent.py ExtendedLiveTests -v
```

The three live-test flags are independent. Set all three for the full suite.
Live tests use the shared GPU sequentially; prefer offline tests during routine
development and use live cases when checking model behavior.

Each live case creates a fresh temporary workspace and launches a separate agent
process. The tests check exact creation, selective deletion, the expected answer
of 12 notebooks, and summary facts and word count. The summary is printed to
stderr for human review of accuracy and clarity; keyword checks alone do not
fully evaluate summary quality. Temporary workspaces are cleaned up afterwards.

This is an initial test set, not yet a comprehensive grading-readiness suite.
Keep credentials, caches, virtual environments, and test workspaces out of the
submission. No dependency installation file is necessary for standard-library-only code.

## Manual console tests without a helper function

These commands invoke the agent directly, just as the grader does. No
`run_prompt` function is needed. Begin in the submission folder containing
`run_agent.sh`, including when testing an extracted ZIP on another machine.
Start Bash first:

```bash
bash
```

Then paste this setup block in that Bash session:

```bash
submission_dir="$(pwd -P)"
export OPENAI_BASE_URL='https://llm.montii.me/v1'
export MODEL='Qwen/Qwen3-14B'
read -r -s -p 'Paste your course API key, then press Enter: ' QWEN_API_KEY
printf '\n'
export QWEN_API_KEY
```

Paste just the key without quotes; do not echo it. The exported configuration
applies to each agent invocation in this session.

Paste one complete command block at a time and wait for the Bash prompt before
pasting the next block. Each example creates a fresh temporary workspace and
prints the agent's response and exit code. Successful tasks should return
`"status": "success"` and exit code `0`; errors should return `"status": "error"`
and a nonzero code. The exit-code line comes from your terminal command, not the
agent. Optional inspection commands are provided separately so you can review
results yourself.

### Create a file with exact content

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
printf 'keep me\n' > keep.txt
bash "$submission_dir/run_agent.sh" \
  'Create data/colors.txt containing exactly red, green, and blue on separate lines, with no trailing newline.'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Optionally inspect the result:

```bash
cat data/colors.txt
printf '\n'
cat keep.txt
```

You should see red, green, and blue on separate lines; keep.txt should still contain `keep me`.

### Delete a file

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
mkdir -p archive/2024
printf 'old\n' > archive/2024/january.log
printf 'keep\n' > archive/2024/february.log
bash "$submission_dir/run_agent.sh" \
  'Delete archive/2024/january.log, but leave the other log files unchanged.'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Optionally inspect the result:

```bash
ls archive/2024
cat archive/2024/february.log
```

Only february.log should remain in that folder, containing `keep`.

### Read a file and answer a question

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
printf 'Blue notebooks: 12\nRed notebooks: 7\n' > inventory.txt
bash "$submission_dir/run_agent.sh" \
  'Read inventory.txt. How many blue notebooks are currently in stock?'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Optionally inspect the result:

```bash
cat inventory.txt
```

The input should still list 12 blue notebooks and 7 red notebooks.

The printed JSON `message` must answer **12 blue notebooks**. Different wording
is fine; an incorrect or missing answer fails even when the file is preserved.

### Summarize multiple files

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
mkdir logs
printf 'At 09:00 the checkout service stopped working.\n' > logs/start.txt
printf 'At 09:15 engineers identified an expired certificate as the cause.\n' > logs/update.txt
printf 'At 09:30 engineers renewed the certificate and restored checkout.\n' > logs/resolution.txt
bash "$submission_dir/run_agent.sh" \
  'Read logs/start.txt, logs/update.txt, and logs/resolution.txt. Write reports/incident_summary.txt in fewer than 100 words covering all three times, the cause, and the resolution.'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Optionally inspect the result:

```bash
cat reports/incident_summary.txt
printf '\n'
wc -w reports/incident_summary.txt
cat logs/start.txt logs/update.txt logs/resolution.txt
```

Review the summary, word count, and original source contents.

Check the actual summary for these expected facts: checkout stopped at **09:00**;
an **expired certificate** was identified at **09:15**; renewal restored checkout
at **09:30**. It should be concise, cover all requested facts, and add no unsupported
claims. The grader evaluates the summary file; the JSON message need not repeat it.

### Request a missing file

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
bash "$submission_dir/run_agent.sh" \
  'According to missing_schedule.txt, when is the final presentation?'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Expect an error JSON, a nonzero exit code, and no invented date. This is an extra
robustness check, not one of the four required successful task types.


### Try your own request or an empty file

Create a fresh workspace, prepare any input files, and replace the prompt below
with your request. This example requests an empty file:

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
bash "$submission_dir/run_agent.sh" 'Make an empty file named ready.flag.'
agent_exit=$?
printf 'Exit code: %s\n' "$agent_exit"
```

Optionally inspect the result:

```bash
ls -l ready.flag
cat ready.flag
```

The file should have size 0, and cat should print nothing.

Temporary workspaces remain outside the submission so you can inspect them.
Return to the submission folder when finished:

```bash
cd "$submission_dir"
```

After all live testing, run `unset QWEN_API_KEY` to remove the key from this Bash
session. The optional helper walkthrough below additionally validates JSON
structure, exit code, and elapsed time automatically.

## Manual walkthrough: test like the grader

Use this to see the agent's actual output and inspect the files yourself. Each
case below uses a fresh temporary workspace and a new agent process. Run the
blocks in order in the same terminal session. These commands target Bash on
macOS or Linux; the example project path is for this Mac.

### 1. Configure your terminal once

Start Bash (even if your usual Mac shell is zsh):

```bash
bash
```

Then configure the submission location and model. Change the `cd` path if you
moved the project or are testing an extracted submission archive.

```bash
cd '/Users/zakiaraujo/Documents/Boston University/Undergrad 4/F26/CS598/Homework-1'
submission_dir="$(pwd -P)"
export OPENAI_BASE_URL='https://llm.montii.me/v1'
export MODEL='Qwen/Qwen3-14B'
read -r -s -p 'Paste your course API key, then press Enter: ' QWEN_API_KEY
printf '\n'
export QWEN_API_KEY
python3 --version
```

Paste just the key, without quotation marks or the surrounding notes from the
key document. Input is hidden, and the key is not written to a file or included
in a shell command in your history. Python must be version 3.10 or later.

### 2. Define a helper to check the grading contract

Paste this function once. It runs the exact launcher pattern, saves stdout and
stderr outside the task workspace, and verifies the JSON and exit code. It also
reports elapsed time. The helper's own diagnostic lines are not agent output.

```bash
run_prompt() {
    local prompt="$1"
    local expected_status="${2:-success}"
    local result_dir
    local agent_exit
    local started_at
    local elapsed
    result_dir="$(mktemp -d)"
    started_at=$SECONDS
    OPENAI_BASE_URL="$OPENAI_BASE_URL" \
    MODEL="$MODEL" \
    QWEN_API_KEY="$QWEN_API_KEY" \
        bash "$submission_dir/run_agent.sh" "$prompt" \
        > "$result_dir/stdout.json" 2> "$result_dir/stderr.txt"
    agent_exit=$?
    elapsed=$((SECONDS - started_at))
    printf '\nAgent stdout:\n'
    cat "$result_dir/stdout.json"
    printf '\nExit code: %s; elapsed: %s seconds\n' "$agent_exit" "$elapsed"
    printf 'Captured output: %s\n' "$result_dir"
    if [ -s "$result_dir/stderr.txt" ]; then
        printf 'Agent stderr:\n'
        cat "$result_dir/stderr.txt"
    fi
    python3 - "$result_dir/stdout.json" "$agent_exit" "$elapsed" "$expected_status" <<'PY'
import json
from pathlib import Path
import sys

result = json.loads(Path(sys.argv[1]).read_text())
code, elapsed = int(sys.argv[2]), int(sys.argv[3])
expected = sys.argv[4]
assert isinstance(result, dict), 'Output must be a JSON object'
assert set(result) == {'status', 'message'}, 'Unexpected or missing fields'
assert all(isinstance(value, str) for value in result.values()), 'Both fields must be strings'
assert result['status'] in ('success', 'error'), 'Invalid status'
assert result['status'] == expected, result
assert (code == 0) == (expected == 'success'), 'Incorrect exit code'
assert elapsed < 180, 'Time limit exceeded'
print('PASS: JSON, exit code, and elapsed time. Inspect task results next.')
PY
}
```

`json.loads` rejects extra progress text, Markdown fences, and multiple JSON
objects. If you see an assertion failure or traceback, that check failed. A
contract pass alone does not establish that the task was completed correctly;
the checks below inspect the files and answers too.

### 3. Create a file with exact content

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
printf 'keep me\n' > keep.txt
run_prompt 'Create data/colors.txt containing exactly red, green, and blue on separate lines, with no trailing newline.'
python3 - <<'PY'
from pathlib import Path
assert Path('data/colors.txt').read_bytes() == b'red\ngreen\nblue'
assert Path('keep.txt').read_bytes() == b'keep me\n'
assert {p.as_posix() for p in Path('.').rglob('*')} == {'keep.txt', 'data', 'data/colors.txt'}
print('PASS: exact content, missing parent created, unrelated file preserved.')
PY
```

### 4. Delete only the requested file

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
mkdir -p archive/2024
printf 'old\n' > archive/2024/january.log
printf 'keep\n' > archive/2024/february.log
run_prompt 'Delete archive/2024/january.log, but leave the other log files unchanged.'
python3 - <<'PY'
from pathlib import Path
assert not Path('archive/2024/january.log').exists()
assert Path('archive/2024/february.log').read_bytes() == b'keep\n'
assert {p.as_posix() for p in Path('.').rglob('*')} == {'archive', 'archive/2024', 'archive/2024/february.log'}
print('PASS: only the requested file was deleted; parent directories remain.')
PY
```

### 5. Read a file and answer a question

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
printf 'Blue notebooks: 12\nRed notebooks: 7\n' > inventory.txt
run_prompt 'Read inventory.txt. How many blue notebooks are currently in stock?'
python3 - <<'PY'
from pathlib import Path
assert Path('inventory.txt').read_bytes() == b'Blue notebooks: 12\nRed notebooks: 7\n'
assert {p.name for p in Path('.').iterdir()} == {'inventory.txt'}
print('PASS: input unchanged. Check that the JSON message answers 12 blue notebooks.')
PY
```

The printed JSON `message` must answer **12 blue notebooks**. Different wording
is fine; an incorrect or missing answer fails even when the file is preserved.

### 6. Summarize multiple files

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
mkdir logs
printf 'At 09:00 the checkout service stopped working.\n' > logs/start.txt
printf 'At 09:15 engineers identified an expired certificate as the cause.\n' > logs/update.txt
printf 'At 09:30 engineers renewed the certificate and restored checkout.\n' > logs/resolution.txt
run_prompt 'Read logs/start.txt, logs/update.txt, and logs/resolution.txt. Write reports/incident_summary.txt in fewer than 100 words covering all three times, the cause, and the resolution.'
python3 - <<'PY'
from pathlib import Path
expected_sources = {
    'logs/start.txt': b'At 09:00 the checkout service stopped working.\n',
    'logs/update.txt': b'At 09:15 engineers identified an expired certificate as the cause.\n',
    'logs/resolution.txt': b'At 09:30 engineers renewed the certificate and restored checkout.\n',
}
for name, content in expected_sources.items():
    assert Path(name).read_bytes() == content, f'Source changed: {name}'
summary = Path('reports/incident_summary.txt').read_text()
assert 0 < len(summary.split()) < 100
assert {p.as_posix() for p in Path('.').rglob('*')} == set(expected_sources) | {'logs', 'reports', 'reports/incident_summary.txt'}
print(summary)
print(f'Word count: {len(summary.split())}')
print('PASS: output exists, length satisfied, sources preserved. Review facts below.')
PY
```

Check the actual summary for these expected facts: checkout stopped at **09:00**;
an **expired certificate** was identified at **09:15**; renewal restored checkout
at **09:30**. It should be concise, cover all requested facts, and add no unsupported
claims. The grader evaluates the summary file; the JSON message need not repeat it.

### 7. Check a failure case

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
run_prompt 'According to missing_schedule.txt, when is the final presentation?' error
```

Expect an error JSON, a nonzero exit code, and no invented date. This is an extra
robustness check, not one of the four required successful task types.

### Try your own prompts and finish testing

For each variation, create a fresh workspace with `task_dir="$(mktemp -d)"` and
`cd "$task_dir"`, prepare any inputs, then call `run_prompt 'your full prompt'`.
Write down the expected content or facts before running it. Try empty files,
spaces and Unicode in filenames, different question wording, and summaries with
different word limits. For creation, compare bytes to detect unwanted newlines.

The manual examples leave their temporary workspaces and captured output in the
system temporary directory so you can inspect them. They are outside the
submission folder. To return to the project and remove the key from this Bash
session's environment:

```bash
cd "$submission_dir"
unset QWEN_API_KEY
exit
```

The course uses hidden cases. These checks reproduce the invocation and observable
checks, but passing them does not guarantee a particular grade. Before submitting,
repeat the checks against an extracted clean archive to verify its contents too.
