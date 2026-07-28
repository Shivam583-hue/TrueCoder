# TrueCoder architecture

TrueCoder is a small agent runtime around an LLM.

A user message starts a turn. The model may answer directly or request tools. Tool results are added to the active turn, and the model is called again until it produces a final response.

## Core structure

```text
UI
 ↓
Agent
 ├─ Context and state
 ├─ LLM client
 └─ Tools

UI
 ↓
Session manager
 ├─ Agent state
 └─ SQLite session store
```

The UI handles presentation and user input.

The agent owns orchestration.

The LLM client translates provider responses into internal types.

Tools are independent units that validate arguments, perform work, and return structured results.

Dependencies should point toward the core. Tools must not depend on the agent, client, or UI.

## Conversation model

A session contains completed turns.

A turn contains everything caused by one user message:

* the user message
* model responses
* tool calls
* tool results
* the final assistant response

Only completed turns enter history. Interrupted or invalid turns are discarded.

Tool calls and their results are atomic. Context trimming must never split them.

## Context

Each model request includes:

* the system prompt
* recent completed turns
* the complete active turn

History is selected as one contiguous recent block. Older turns are removed whole. Selection stops when the next turn does not fit.

## Agent loop

```text
build context
→ call model
→ collect text or tool calls
→ execute tools
→ record results
→ repeat
→ commit final response
```

The loop has a maximum iteration limit.

Model text is only committed once the response is complete. Request failures abort the active turn. Tool failures become structured results when the model can reasonably recover from them.

The detailed turn lifecycle is:

1. The user sends a message.
2. The agent starts an active turn and records the user message as pending.
3. The context builder creates a model request.
4. The model returns text, tool calls, or both.
5. Requested tools are marked outstanding, executed, and recorded in order.
6. The model is called again after every tool-call batch is resolved.
7. A final assistant response completes the pending turn.
8. The complete pending message group is committed as one turn.
9. Active-turn state is cleared.

## Sessions

Sessions persist completed turns, not flattened messages or UI widgets.

The session manager coordinates the current `AgentState` with a project-scoped
SQLite store. The store lives in the operating system's user-data directory, so
session data never enters the repository.

Each application launch creates an active session immediately. After an
`AGENT_END` event, the manager atomically appends any completed turns that are
not yet stored. Errors, cancellations, pending approvals, unresolved tool calls,
and streamed partial text are never persisted.

Restoration is transactional:

* persisted turns are decoded and validated before active state changes
* every turn must begin with a user message and end with assistant text
* tool calls and results must remain matched and ordered
* invalid stored data leaves the existing conversation untouched
* switching replaces completed history only after the target session loads

Sessions are isolated by canonical project root. A repository cannot list,
resume, rename, or delete another repository's sessions. Deleting the active
session creates a new empty session so the application always has an active
session. Empty sessions are temporary placeholders: creating another session,
switching away, or closing the application automatically removes an active
session that still has zero completed turns.

The TUI reconstructs transcript widgets from durable model messages. Focus,
scroll position, expanded tool details, elapsed timing, token usage, and other
presentation-only state are intentionally not persisted.

## Tools

Tool definitions, calls, arguments, and results are typed values.

Arguments cross the model boundary as JSON and are validated before approval or execution.

Tools are registered explicitly. Restricted tools, especially filesystem tools, must enforce their own security boundaries.

The shipped filesystem tools share one sensitive-path policy and are rooted at
the canonical project root:

* `read_file` returns bounded UTF-8 line ranges and never exposes paths outside
  the project or known credential locations
* `write_file` creates or completely replaces one UTF-8 text file, requires an
  existing parent directory, rejects symlinks and sensitive paths, and limits
  content to 32 KiB
* `edit_file` replaces exact text in an existing UTF-8 file, either once when
  the match is unique or everywhere when explicitly requested
* `list_dir` returns at most 500 immediate children of one directory without
  recursing
* `glob` finds at most 500 files or directories with rooted `*` and recursive
  `**` path patterns
* `grep` searches one file or a directory tree with a Python regular expression
  and returns at most 200 matching lines with paths and line numbers

`list_dir`, `glob`, and `grep` do not interpret `.gitignore`. They exclude the
shared sensitive-path set, never follow symlinks while traversing, and use
explicit scan limits so a model request cannot walk an unbounded tree.
`list_dir` scans no more than 5,000 entries. `glob` and `grep` scan no more than
20,000 entries.

`write_file` never appends, makes partial edits, creates directories, or writes
non-regular files. It writes a temporary file beside the destination, flushes
it, preserves existing permissions when replacing a file, and uses
`os.replace()` so failure cannot expose a partially written destination. Its
result reports only the relative path, whether the file was created, and the
UTF-8 byte count; the original content is not duplicated in the result.

`edit_file` is deliberately an exact-text operation rather than a line-number
patch. Its required arguments are `path`, `old_text`, `new_text`, and
`replace_all`. With `replace_all=false`, the old text must appear exactly once;
zero matches produce `text_not_found`, and multiple matches produce
`ambiguous_match`. An empty `new_text` deletes the exact match. Both edit
fragments are limited to 32 KiB, and the existing and resulting files are
limited to 1 MiB.

Before replacing an edited file, `edit_file` checks that the file still has the
same device, inode, size, and modification timestamp it had when read. A
concurrent change becomes `file_changed` instead of being overwritten. The
replacement is written and flushed in the same directory, keeps the original
permission bits, and is installed with `os.replace()`. This gives readers either
the old complete file or the new complete file, never a partially written file.

`grep` searches full lines but truncates returned display lines to 500
characters. Files larger than 1 MiB, binary-looking files, and non-UTF-8 files
are skipped during a recursive search. Its pattern is a Python regular
expression, so callers can use anchors, groups, and inline flags such as
`(?i)`. `glob` is for path-name patterns instead: `*` stays within one directory
level, while `**` may cross levels.

All shipped filesystem tools require the normal awaited approval interaction.
Successful and failed calls remain part of the current turn and are persisted
with that turn once the model produces its final response. The TUI renders
compact completed summaries such as `Listed src · 4 entries`,
`Matched . · 12 matches`, `Searched src · 3 matches`, and
`Edited src/app.py · 1 replacement`; the same summaries are reconstructed when
a session is resumed.

## Approval

Approval is an awaited request-response interaction.

The agent asks an injected approval handler for a decision and pauses until it receives one. The UI may display approval events, but it does not own approval state or agent execution.

Approval policy belongs to the tool. Orchestration belongs to the agent. Presentation belongs to the UI.

## Design rules

Keep these invariants stable as the codebase grows:

* completed history contains only valid turns
* context is recent, contiguous, and turn-based
* tool calls always have matching results
* provider-specific behavior stays inside the client
* tools do not depend on outer layers
* the UI does not contain agent logic
* session saves happen only at completed-turn boundaries
* restoring a session cannot partially replace agent state
