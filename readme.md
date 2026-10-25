# CS598 Homework 1 — Programming, debugging, file and CSV agent

This agent implements its own Qwen tool-calling loop using only Python's standard
library. No agent SDK or additional Python packages are required.

## Requirements and files

- Python 3.10 or later and Bash on Linux or macOS.
- `requirements.txt`: documents that no third-party packages are required; no installation step is needed.
- `run_agent.sh`: entry point; resolves source code relative to itself and preserves the task working directory.
- `agent.py`: tool loop, workspace operations, completion checks, and final JSON output.
- `completion_checks.py`: requested-output checks, incident-report evidence, and pending program-test tracking.
- `model_client.py`: streaming Chat Completions client with thinking disabled.
- `workspace_tools.py`: bounded log/source inspection and Python execution tools.
- `python_runner.py`: read-only execution guard for generated Python and analysis snippets.
- `sessions.py`: persistent conversation history and locking, isolated by workspace and session ID.
- `csv_tools.py`: CSV reading, decimal calculations, updates, filtering, and copying.
- `scripts/test_connection.py`: one small endpoint connectivity check.
- `tests/test_agent.py`: generated test inputs, expected results, offline checks, and optional live tests.
- `tests/test_sessions.py`: separate-process persistence and session isolation checks with a scripted model.
- `tests/test_csv.py`: CSV checks and optional live Checkpoint 2 conversations.
- `tests/test_memory_regressions.py`: reported memory-recall and session-isolation failures, tested offline and live.
- `tests/test_final_tools.py`: inspection, execution, streaming, limits, and completion regressions.
- `tests/test_final_live.py`: generated programs and reports checked against independent expected results.
- `tests/final_fixtures.py` and `tests/fixtures/`: reproducible synthetic inputs and task prompts.
- `scripts/prepare_final_fixture.py`: creates a manual final-checkpoint workspace.
- `scripts/run_tests.py`: offline or full live suite, with spacing between live turns.
- `walkthrough.md`: a step-by-step explanation of the harness and an example request.

Tested environment: macOS 15.7.3 with Python 3.13.7. Linux has not yet been tested.

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
290-second limit. Corrupt history produces an error instead of silently losing
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

The complete suite has 75 offline tests and 21 optional live tests, including final-checkpoint checks. To run only the three live
Checkpoint 2 conversations (10 separate agent invocations, sequentially):

```bash
RUN_CHECKPOINT2_LIVE_TESTS=1 python3 tests/test_csv.py Checkpoint2LiveTests -v
```

These cover sum/average/copy, interleaved sessions, and row arithmetic/update/filter.

### Conversational memory regression tests

The grading report exposed a plain-text response handling bug: Qwen answered the
memory question correctly, but a harness retry replaced it with a generic task
confirmation. The regression tests reproduce this response sequence and verify
that the requested fact reaches the final JSON `message`.

Run the five offline regression tests, with no API key required:

```bash
python3 tests/test_memory_regressions.py MemoryRegressionTests -v
```

With the API environment configured, run the exact reported conversations:

```bash
RUN_CHECKPOINT2_LIVE_TESTS=1 python3 tests/test_memory_regressions.py MemoryLiveTests -v
```

These live tests remember Cedar in session 6101, and Harbor/Meadow in sessions
6404/6405, then check recall in separate agent processes. Each case uses a fresh
workspace and also checks preservation of an unrelated binary file and empty
directory. The offline tests additionally cover quotes/Unicode, final history
content, and plain text following a failed file operation. All are included in
the full suite, with live tests controlled by the existing Checkpoint 2 flag.

Session mode now accepts a plain-text final answer and places it in `message`
without a formatting retry. The harness still prints only the required JSON.
History stores the answer text instead of the CLI status object. If a correction
is needed, the retry explicitly asks to preserve the requested answer and facts.

The old `hw1-1.zip` and `hw1-2.zip` archives are earlier snapshots and do not
contain the current fixes. Use the final `hw1-3.zip` archive after packaging the
current source. Never submit generated session histories or credentials.

## Checkpoint 3: programming and debugging

The same loop handles new programming and debugging prompts. Programming
requests first ask Qwen for an interface, approach and verification plan, then
use a focused system prompt with general implementation/testing guidance. Written
Python source is compiled immediately so syntax errors reach the model before
it spends another call attempting execution. It has no built-in
`tally.py` solution, incident IDs, or expected report values. Those are confined
to test fixtures. `list_files`, `read_lines`, and `search_file` inspect the actual
workspace; `write_file` creates the requested deliverable. `replace_text` makes
a unique, exact snippet replacement for a small source correction while preserving
all other file bytes. Edited Python source must be tested again. After repeated
failed program runs, the agent must replace the full source rather than continue
an ineffective sequence of small patches. Search returns up to
20 matches with context and a continuation line for larger results. The agent is
instructed to follow the failed incident's identifiers and check all matches,
including later failures following successful attempts.

`run_python` executes a saved program with arguments and optional stdin.
An optional `reference_code` runs an independent Python calculation against the
same inputs and compares its actual stdout with the saved program's stdout.
Both results and `outputs_match` are returned; an incorrect reference never
erases the program's output. A mismatch blocks completion until the same check
is rerun successfully with a corrected program or reference. Deliberate error
tests use `expected_exit_code` and should omit the reference calculation; a correct
rejection is accepted even if an optional reference is broken. Positive-output
disagreements receive a focused Qwen review in a fresh context before the next
repair action, distinguishing candidate defects from broken reference calculations.
This review uses the same Qwen model endpoint and counts toward the 120-call cap.
This avoids asking the model to guess or manually
transcribe expected totals. Independent live tests still fail on any incorrect
output. Explicit requests to print compact JSON also receive a format check.
`analyze_python` executes a short snippet for independent calculations or report
validation. A programming task cannot report success until every Python source
file it wrote has been run with the expected exit code after its last edit. This is a minimum
completion check, not proof of program correctness; live tests separately compare
outputs with expected results on multiple inputs.

Explicitly named output files must exist and have been written during the task;
inspection alone cannot satisfy a programming request. Explicit JSON deliverables
must parse; a filename extension alone does not change an exact-text request.
For incident reports with an enumerated schema, the keys must match the
prompt, required values cannot be empty, and strings requested verbatim from logs
must appear in inspected evidence. A focused debugging prompt follows each failed
request through its service-specific identifiers and checks successful branches
as well as the causal failure. These checks contain no fixed assignment answers.
Failed program commands stay pending across unrelated successful operations;
the same arguments and stdin must be checked again. A failed reference must be
repaired rather than omitted. Completion feedback includes the exact pending
arguments, stdin and reference code so separate inputs cannot be confused.
Checks still cannot prove semantic correctness.
Tasks explicitly requiring rejection of non-integer JSON array elements must
exercise boolean, float and string rejection in separate arrays after the final
source edit, using arguments that also pass a valid-integer control. A source
diagnostic identifies unsafe `isinstance(value, int)` checks and proposes an
exact-type replacement; explicit boolean guards are allowed.
When a CLI specification explicitly requires two-decimal money output and identifies
commands that print a scalar sum/total, those commands also need a successful
zero-result test after the final source edit. The output validator catches plain
`0` as well as other incorrect numeric precision without supplying a task solution.

To repeat final-checkpoint tasks in fresh workspaces while rerunning all earlier
capabilities, use `python3 scripts/run_tests.py --live --repeat-final 3`.
Set `KEEP_FINAL_TESTS=1` to retain final-task workspaces for diagnosing failures;
keep those retained workspaces outside the submission archive.

Execution uses a separate Python process, an environment without the API key,
and a read-only audit guard. Normal file writes, network calls, subprocesses,
and access to session history are blocked. Standard-library imports and workspace
input reads are allowed. Each execution has a maximum 30-second timeout and
bounded output. Invalid-input checks can specify a nonzero `expected_exit_code`. This guard is not a security sandbox for intentionally hostile
Python. Generated programs that require filesystem writes cannot be fully
exercised through this read-only tool. The supplied spending CLI needs only reads.

Requests use streaming responses, disable parallel tool calls to encourage
checking each result before the next action, set `chat_template_kwargs.enable_thinking=false`,
and at most 120 successful completions per invocation. The overall 290-second
timer leaves margin below the final checkpoint's 300-second limit. Transient HTTP
429/502/503/504 responses receive at most two retries within that timer. Large
file reads and CSV previews are bounded; calculations still use the full CSV.
Older large tool results are shortened when conversation text grows. This is a
character-based context heuristic, not an exact Qwen tokenizer count. Large
inputs above 40,000 bytes receive an 8,000-byte preview through `read_file`;
files within the earlier checkpoint's 40,000-byte allowance remain complete.
Programming prompts receive a bounded workspace inventory and CSV preview with
the actual row count and sums of numeric columns, so illustrative example numbers are less likely to be
confused with the supplied data. Rejected malformed tool arguments are stored
as valid JSON in history so they cannot break the next API request.
Programming requests use temperature 0.2 and top-p 0.8 to reduce variation between
independent generations. Repeated syntax-breaking edits count toward the clean-rewrite
threshold. Rejected HTML HTTP 400 responses reporting bad HTTP request syntax
receive bounded retries; ordinary HTTP 400 validation errors do not.
Other requests retain temperature 0. Thinking remains disabled in every request.

The live final tests include a 40,000-row spending CSV (quoted commas, refunds,
mixed category case, ties, date boundaries, alternate input paths), a different
sliding-window programming problem, and two noisy four-service log incidents
with different identifiers and causal classes. Tests check exact outputs, JSON
keys/types, and preservation of all supplied inputs and unrelated files. The six
unpublished programming requirements and six unpublished report fields are not
available: our extra report fields are clearly labeled local examples, not claims
about the hidden grader. Full runtime prompts remain authoritative.

### Manual final tests, without a test helper or assertions

First configure Bash, `submission_dir`, and the API environment using the setup
block under **Manual console tests without a helper function** below. Keep
`submission_dir` pointing to the folder containing `run_agent.sh`, not a temporary
workspace. The preparation script only writes synthetic input files; the agent
is then invoked directly, as it is by the grader.

Programming:

```bash
task_dir="$(mktemp -d)"
python3 "$submission_dir/scripts/prepare_final_fixture.py" programming "$task_dir"
cd "$task_dir"
cat TASK.md
bash "$submission_dir/run_agent.sh" "$(cat TASK.md)" 9101
echo "Exit code: $?"
python3 tally.py count
python3 tally.py total
python3 tally.py total --category fOoD
python3 tally.py top 2
python3 tally.py range --from 2026-09-02 --to 2026-09-03
```

Expected program output, in command order: `40000`, `250000.00`, `100000.00`,
then two lines `food<TAB>100000.00` and `travel<TAB>100000.00`, then
`count=20000 total=97500.00`. Inspect `tally.py` if any result differs. Check a new
input to ensure the code did not memorize the fixture:

```bash
printf 'date,description,category,amount\n2026-01-01,"A, B",Other,-3.00\n' > custom.csv
python3 tally.py total --file custom.csv
python3 tally.py top --file custom.csv 8
```

Expected: `-3.00`, then `other<TAB>-3.00`.

Debugging:

```bash
task_dir="$(mktemp -d)"
python3 "$submission_dir/scripts/prepare_final_fixture.py" debugging "$task_dir"
cd "$task_dir"
cat TASK.md
bash "$submission_dir/run_agent.sh" "$(cat TASK.md)" 9102
echo "Exit code: $?"
cat REPORT.json
printf '\n'
```

Compare the report with the expected object printed by the preparation command.
That object is not written into the agent's workspace. Inspect the evidence:

```bash
grep 'ORD-77341' logs/gateway.log
grep 'gw-8417' logs/gateway.log
grep 'tr-aa11bb22' logs/orders.log
grep 'hold-6543' logs/inventory.log
grep 'pi_test_main271' logs/payments.log
```

The first matching order line is a successful decoy; the failed request must be
traced through its own identifiers. The expected causal class is `LeaseRegistry`.
Repeat in another fresh directory with `debugging-alternate` to test different
identifiers and the causal class `ReservationIndex`.

### Recheck the reported Part 2 failures directly

Keep the API configuration and `submission_dir` from setup. Paste these commands
in order; no CSV file or file operation is involved:

```bash
task_dir="$(mktemp -d)"
cd "$task_dir"
bash "$submission_dir/run_agent.sh" 'My project code name is Cedar. Remember it for this conversation.' 6101
bash "$submission_dir/run_agent.sh" 'What is my project code name?' 6101
bash "$submission_dir/run_agent.sh" 'My project code name is Harbor. Remember it for this conversation.' 6404
bash "$submission_dir/run_agent.sh" 'My project code name is Meadow. Remember it for this conversation.' 6405
bash "$submission_dir/run_agent.sh" 'What is my project code name?' 6404
bash "$submission_dir/run_agent.sh" 'What is my project code name?' 6405
```

The recall messages must actually contain **Cedar**, **Harbor**, and **Meadow**
for their respective sessions. A generic completion message is a failure even if
the status says success.

### Submission

Submit the complete updated framework using the course Slack command
`submit hw1-3`, due October 25, 2026 at 23:59:59 Eastern. The final archive must
have `run_agent.sh` at its root, all Python modules, documentation, test fixtures,
and expected results. No package installation is needed because every dependency
is in the Python standard library. Exclude credentials, old ZIPs, session history,
caches, virtual environments, and generated task workspaces. Both earlier
checkpoints are regraded on this final source.

## How it works

The harness sends the prompt and tool definitions to `/chat/completions`. Qwen
requests `read_file`, `write_file`, or `delete_file`; Python validates and executes
each operation and returns its result to Qwen. The model calls `finish` with the
final status and answer. The harness also accepts a strictly validated JSON final
result if the endpoint renders completion as text. It also preserves plain-text
session answers in `message` and stores answer text in history. Without a session ID, success
requires prior successful tool execution in that call. Session turns may answer
from remembered context or acknowledge an instruction without a new file operation.

Paths must stay within the task workspace. Symlinks are rejected, writes use
UTF-8 without automatic newlines, missing parent directories are created, and
deletion never removes directories. The harness uses a 290-second overall timer
and at most 120 model calls. It disables thinking as in the course quick start.
Each response is capped at 8,192 output tokens; oversized responses fail explicitly.
Set `AGENT_DEBUG=1` to emit model completion text, tool names, and tool errors to
stderr for troubleshooting; this can include task data.

## Tests

### Quick automated check

After setting your API key as shown in the manual walkthrough below, run all
local and live tests from the submission folder:

```bash
python3 scripts/run_tests.py --live
```

Expect `Ran 74 tests` and `OK`, with no skipped tests. This command enables all
four live-test flags and spaces separate agent invocations by 10 seconds to reduce
rate limits on the shared endpoint. It can take several minutes. For offline-only
checks use `python3 scripts/run_tests.py`; 19 live cases will be skipped.
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

The four live-test flags are independent. `RUN_FINAL_LIVE_TESTS=1` enables the
new generation tasks; `scripts/run_tests.py --live` enables all four groups.
Live tests use the shared GPU sequentially; prefer offline tests during routine
development and use live cases when checking model behavior.

Each live case creates a fresh temporary workspace and launches a separate agent
process. The tests check exact creation, selective deletion, the expected answer
of 12 notebooks, and summary facts and word count. The summary is printed to
stderr for human review of accuracy and clarity; keyword checks alone do not
fully evaluate summary quality. Temporary workspaces are cleaned up afterwards.

These tests exercise the public requirements and additional regressions, but do
not include the course's unpublished cases.
Keep credentials, caches, virtual environments, and test workspaces out of the
submission. `requirements.txt` intentionally lists no third-party dependencies.

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
assert elapsed < 300, 'Final-checkpoint time limit exceeded'
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

### Expanded Checkpoint 3 test coverage

See `tests/fixtures/coverage.md` for the requirement-to-test map, the six live
tasks, and the distinction between published requirements and explicitly defined
local practice extensions. New tally checks use an independent integer-cent
oracle and direct CLI execution with site-packages disabled.
