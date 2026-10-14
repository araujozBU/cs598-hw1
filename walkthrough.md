# Understanding the harness

The harness is the Python code that connects the language model to executable
tools. Qwen interprets a request and chooses operations; Python performs those
operations and reports what happened.

## 1. Start in the task workspace

The grader runs `bash /path/to/submission/run_agent.sh "the prompt"` from a
separate task directory. The launcher locates `agent.py` beside itself without
changing the working directory. This matters: the code lives in the submission,
but the files it works on live in the task directory.

`main()` reads `sys.argv[1]`, constructs `Workspace(Path.cwd())`, and starts the
170-second timer. It also catches failures so they produce the required JSON.

## 2. Tell Qwen what it can do

`SYSTEM_PROMPT` describes the task rules. `TOOLS` describes each operation's name
and arguments using JSON schemas. These descriptions do not execute anything.
The actual implementations are in `Workspace.execute()`.

The model can request `read_file`, `write_file`, `delete_file`, or `finish`.
There is no separate summary tool: Qwen reads source files, composes a summary,
and passes that summary to `write_file`.

## 3. Send the conversation to the model

`model_request()` reads the endpoint, model name, and key from the environment.
It sends an HTTP POST to `/chat/completions`, including the conversation and tool
definitions. It turns connection problems and invalid responses into errors the
entry point can report. The key is an HTTP authorization header, not part of the
model's conversation.

## 4. Execute a tool and return the result

Suppose the request is: "Read inventory.txt. How many blue notebooks are in stock?"

1. Qwen requests `read_file` with arguments `{"path":"inventory.txt"}`.
2. Python parses those arguments. `file_path()` checks that the path stays inside
   the workspace and does not use a symbolic link.
3. Python reads the file and returns its contents as a tool result in the conversation.
4. The next model request includes that result. Qwen can now answer from the file.
5. Qwen calls `finish` with, for example,
   `{"status":"success","message":"There are 12 blue notebooks in stock."}`.

The repeated exchange is implemented by `run_agent()`. It allows up to 16 model
turns. Calls without a session ID start a new conversation. With a session ID,
previous turns are loaded from workspace-local storage before the new prompt is
appended, and completed turns are saved for the next process.

## 5. Produce the grader's output

`final_result()` validates exactly two string fields: `status` and `message`.
`main()` serializes the result with `json.dumps`, preserving quotes and Unicode
correctly, and selects exit code 0 for success or 1 for an error. Diagnostic output
goes to stderr; stdout is reserved for the single result object.

The endpoint sometimes returns a textual `finish` followed by JSON instead of a
formal tool call. The loop handles that observed format too. It will not accept a
success result without a prior successful file operation in independent mode.
Session mode also permits answers from earlier context and acknowledgment of
instructions that do not require a file operation.

## What the checks establish

Python directly checks paths, argument types, JSON output, and the time limit.
The model is responsible for choosing the correct requested files, interpreting
their contents, and producing an accurate answer or summary. A successful tool
call alone does not prove that the entire user's request was fulfilled. That is
why tests also inspect final files, preserved inputs, and expected facts.

Offline tests isolate Python behavior with temporary files and scripted model
responses. Live tests exercise Qwen and the full Bash entry point in a fresh
workspace per case. Passing those examples provides evidence of behavior; it
does not guarantee passing the course's hidden prompts.

## Checkpoint 2 additions

`main()` reads the second argument as the session ID. `SessionStore` acquires a
per-session lock, loads that session's messages, and saves the updated history
after the turn completes. `run_agent()` retains user messages, model tool calls,
tool results, and final answers. A completed `finish` call is stored as an
assistant answer so the next request has no unmatched tool call. The system
prompt is supplied fresh each time rather than being stored on disk.

`csv_tools.py` adds structured CSV operations to the same tool loop. For example,
Qwen selects the revenue column and asks `calculate_csv` for an average; Python
parses the CSV and computes it with Decimal. Qwen uses the returned result in its
answer. For a later request such as "Save a copy of the table," the conversation
provides the filename, while `copy_csv` handles the actual file copy.

Session IDs are isolated within each workspace. The model cannot use its file
tools to read or modify the reserved `.agent_sessions` directory. Task data files
remain shared within the workspace; separate conversation history does not create
a separate filesystem for each session.
