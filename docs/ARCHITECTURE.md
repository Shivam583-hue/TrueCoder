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
 ├─ Approval service
 ├─ Plan store
 └─ Tools
     ├─ Workspace filesystem tools
     │   ├─ Mutation preview
     │   └─ Mutation audit store
     ├─ Plan adapter
     ├─ Memory adapters
     │   └─ Workspace-scoped note store
     ├─ Web fetch adapter
     │   └─ URL policy, pinned client, extraction
     ├─ Code intelligence adapters
     │   └─ LSP manager, client, header framing
     ├─ Tool server adapters
     │   └─ MCP manager, client, schema bounds, line framing
     ├─ Shared JSON-RPC stdio transport
     └─ Shell adapter

Shell adapter
 ↓
Execution service
 ├─ Execution context
 ├─ Cancellation registry
 ├─ Durable audit service
 ├─ Pure policy, environment, and output components
 ├─ Backend discovery and capability selection
 └─ Platform backends
     ├─ POSIX local
     ├─ Linux Docker sandbox
     └─ Windows local (Job Object)

UI
 ↓
Session manager
 ├─ Agent state
 └─ SQLite session store

Agent
 ↓
Hook runner
 └─ Execution service, pre-authorised

Agent
 ↓
Checkpoint service
 ├─ Temporary-index snapshot
 ├─ Snapshot refs inside the repository
 └─ Tree-against-tree turn comparison
```

The UI handles presentation and user input.

The agent owns orchestration.

The LLM client translates provider responses into internal types.

Tools are independent units that validate arguments, perform work, and return structured results.

Dependencies should point toward the core. Tools must not depend on the agent, client, or UI.

Where the agent, a tool, and the UI all need the same domain type, that type belongs in a leaf package that depends on none of them. `truecoder.planning` and `truecoder.mutation` are the current examples: they import nothing from the rest of the package, which is what lets a tool, the context builder, the approval request, and a card share one `Plan` or one `FileDiff` without the tool layer reaching into the agent.

`truecoder.tools` may depend on `truecoder.execution`, and does. The reverse never happens, which is why the mutation evidence store lives beside the tools that write to it rather than inside the execution audit.

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
* durable notes recorded for this workspace, when any exist
* the current task plan, when one exists
* a summary of turns that no longer fit, when one exists

History is selected as one contiguous recent block. Older turns are removed whole. Selection stops when the next turn does not fit.

The system prompt is operating guidance, not a description of the assistant. It
tells the agent to learn how a repository builds and tests itself from the
repository, naming continuous integration first because that is the command the
project actually keeps working; it forbids installing anything to make a command
succeed, because changing the user's environment is the user's decision; it says
plainly that a shortened result means read a narrower range next and never the
same range again; and it states that every action waits on a human approval, so a
speculative call spends someone's attention.
Each rule is there because its absence was observed costing a turn.

The prompt also carries an environment block gathered once at startup: working
directory, operating system, the interpreter TrueCoder is running, and the
workspace virtual environment when one exists.
A model that is told none of this has to discover it by running commands, and it
spends a turn on `python --version` and a failing `pip install` before reaching
the work.
The block is facts only, and it states the absence of a virtual environment as
plainly as its presence, so the model never has to infer anything from silence.
Repository instructions are separate and come after it, because `AGENTS.md` is
where a user records conventions, not where they should have to write down which
machine the agent is on.

Every tool bounds its own output, but nothing bounded those outputs against the
conversation. One shell result may reach 32,769 tokens and one fetched page
20,001, against what was then a default budget of 12,000 for the entire request,
so a single result could exceed the whole budget almost threefold and the budget
was in practice advisory. Tool results are therefore shortened where the request is
assembled: a result over its share of the budget is replaced by a valid envelope
carrying as much of the payload as fits, the original status, the number of
characters dropped, and an instruction to request a narrower range.

That envelope is an instruction to read again, so the budget it is measured
against decides how often the agent re-reads instead of remembering. A budget
small enough that one ordinary file exceeds it turns every large read into a
shortened result and every shortened result into another read. The default is
therefore sized so that a full default `read_file` window of dense source fits
under one result's share, and the ceiling on a single command is sized for a real
test suite rather than a single command.

That shortening applies to the projection only. `AgentState`, the stored
session, and the mutation audit keep the complete result, which is the same
seam the plan uses: `build` returns a fresh list and never edits state. Only
tool messages are shortened; a user's own words are never truncated.

Compaction handles the other half. When history outgrows half the budget, the
oldest turns beyond the two most recent are summarised into a rolling summary
that is injected after the system prompt and labelled as history rather than
instruction. Summarising runs in the agent before a new turn begins, not inside
`build`, which keeps context assembly synchronous and side-effect free, and
means the summarisation request is itself already bounded by the shortening
above. A failed summary leaves history untouched rather than losing a turn.

The plan is not part of history. It is rendered fresh from the plan store on every build and appended after the active turn, so it is always the most recent thing the model sees and can never be evicted by trimming. It is counted against the token budget before history selection begins, so adding a plan tightens how much history fits instead of silently overflowing the limit.

Everything above depends on counting tokens, and counting them requires a
tokenizer that `tiktoken` fetches over the network the first time it is asked
for one.
Building the counter used to load that tokenizer immediately, which put a 3.6 MB
download on the path between typing `truecoder` and seeing anything at all: on a
slow link the terminal stayed blank for half a minute, and with no route to the
network the launch ended in a stack trace rather than an interface.
The counter is therefore constructed empty and loads on first use, so composing
a session touches nothing remote and the interface paints immediately.
Loading happens at most once per process, under a lock so concurrent counting
cannot start two downloads, and the loaded flag is set in a `finally` so a
loader that throws degrades once instead of retrying on every message.

Where that download is kept matters as much as when it happens. The library's
default is the system temporary directory, which several platforms clear on
reboot, so the cost was not paid once but once per boot. It is redirected to the
user cache directory alongside the model catalog, and an explicit
`TIKTOKEN_CACHE_DIR` or `DATA_GYM_CACHE_DIR` is left untouched because an
operator who set one has already made this decision.

A tokenizer that cannot be loaded at all is not fatal. Counting falls back to an
estimate from UTF-8 length, chosen to over-count rather than under-count, since
over-counting shortens a tool result early and under-counting overflows the
model's window. The failure modes are named rather than caught blind: every
`requests` exception descends from `OSError`, a corrupt download raises
`ValueError`, and an absent library raises `ImportError`.

Deferring the load moves the wait to the first message instead of removing it,
so the interactive entry point starts the load on a daemon thread immediately
after composing the session. The interface paints while the download runs, and
by the time a first message is submitted the tokenizer is usually ready. This is
the same rule the model catalog follows: work that is not needed to draw the
first frame does not happen at launch or on a keystroke.

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

## Memory

Memory is what the agent knows about a project after the conversation ends.

The risk it introduces is not storage, it is invisibility: a note recorded weeks
ago changes behaviour today with no obvious cause. Three decisions answer that
directly. Both tools require approval, because a durable change to future
behaviour deserves the same gate as a durable change to a file. Every note is
projected into the request verbatim, so what the model is told is exactly what
the user can read. And `ctrl+n` lists every note with the ability to delete any
of them, which makes the store inspectable rather than merely bounded.

Notes are scoped by canonical workspace identity, so one repository can never
read another's, exactly as sessions and audit evidence are scoped. They live in
the user data directory rather than the repository, because `AGENTS.md` is the
right home for instructions a user maintains and reviews in version control.
Memory is for what the agent worked out; the repository is for what the user
decided. The prompt guidance says so explicitly, and says not to record secrets
or anything the repository already states.

Storage is bounded: a note is a single normalised line, notes are unique per
workspace so repetition cannot accumulate, and the oldest are pruned past a
fixed count. A store that cannot be read never blocks a request, because losing
memory is a degraded reply while failing the turn is no reply at all.

Uniqueness is decided by a key rather than by the note itself. Case and trailing
punctuation are stripped from that key, so `Use tabs`, `use tabs` and `Use tabs.`
are one note and not three competing for a fixed number of slots. The note is
stored as written and shown as written; only the key is normalised. `forget`
resolves through the same key, so dropping a note does not depend on reproducing
its punctuation exactly.

Correction is a first-class operation, because append-only memory degrades in a
particular way. A note that has stopped being true cannot be fixed by recording
the truth beside it: both survive, both are projected, and the model is told two
contradictory things on every subsequent turn. `remember` therefore takes an
optional `replaces`, and removes that note in the same transaction that records
the new one, so a correction either happens completely or not at all.

Failure to match is reported rather than swallowed. A `forget` that matches
nothing returns the notes that do exist, so a model that misquoted can pick the
right one instead of silently believing it succeeded.

A schema migration carries existing stores forward. Version one had no key
column and a unique index on the note text, so migrating backfills the key, keeps
the newest note of each colliding group, and installs the new index. Other
workspaces in the same database are untouched.

## Hooks

A hook runs a command the user configured, around a turn.

The tempting implementation is to spawn it directly, which would be simple and
wrong: it would create a second path for running commands that skips policy,
bounds, and the audit, next to a shell tool that has all three. The whole
premise of the execution plane is that there is one such path.

Running a hook through the execution service raises the opposite problem. That
path asks for approval, and prompting for a formatter forty times a session is
unusable. The resolution is that these are different kinds of authorisation.
Model-directed execution is approved per call because the model chose it. A hook
was chosen by the user, in a file they wrote, before the session began, so it is
pre-authorised by configuration and needs no per-call prompt.

The agent registers the hook's exact call identifier before running it and
releases it immediately afterwards, in a `finally`, so the window is exactly the
one execution. Pre-authorisation is by identity rather than by pattern, so
nothing else can be mistaken for a hook, and a hook that fails cannot leave a
standing grant behind.

Everything else the execution plane provides still applies: policy
classification, a timeout, an output ceiling, and one immutable audit record.
What a hook does not get is isolation. A hook exists to run the user's own
toolchain, and a formatter is not installed in a digest-pinned distroless
sandbox, so hooks use the local backend with host access. That is the same trust
level as a git hook, with bounds and evidence a git hook does not have, and it
is stated plainly rather than implied.

Configuration is strict and fail-closed like the execution policy: an unknown
field or an invalid value disables every hook and reports why. A hook failure is
reported to the user and never blocks the turn, because a formatter that exits
nonzero is information, not a reason to discard the agent's work.

## Checkpoints

Every turn is preceded by a snapshot of the whole workspace, so a turn can be
reversed.

It has to be the whole workspace rather than the recorded mutations. The
mutation audit stores digests for `write_file` and `edit_file`, but `shell`
records nothing there, and a command can write, delete, or reformat anything it
likes. A checkpoint rebuilt from mutation records would restore the reviewed
edits and silently miss everything a command did, which is a worse outcome than
having no checkpoint at all, because it would be believed.

Snapshots are built with git plumbing against a temporary index, so the user's
index, working tree, branch, HEAD, and reflog are never touched. Each snapshot
is a tree plus a commit that no branch points at, kept alive by a ref under
`refs/truecoder/checkpoints`. Metadata travels in the commit message, so git is
the single source of truth and there is no second database to fall out of sync
with the objects it describes. Capture is skipped when the tree is unchanged, so
an idle turn costs nothing, and the newest twenty-five are kept.

Restoring is itself a change, and the destructive part is easy to miss:
restoring to a tree removes files that are tracked now and absent from that
tree, including work staged after the checkpoint was taken. A restore therefore
captures the current state first, reports exactly which paths it removed, and
leaves that safety checkpoint in the list. Undoing a restore is restoring its
safety checkpoint.

Files the agent created without staging them survive a restore, because git
manages tracked content and removing untracked files would risk deleting the
user's own scratch work. The confirmation names what will be removed before
anything happens.

Content is captured and restored verbatim. Git's line-ending conversion is
active by default on Windows, and left alone it would apply on the way into a
snapshot and again on the way out, so restoring could rewrite the endings of
every text file in the workspace and bury the reverted change in a diff that had
nothing to do with the agent. The plumbing therefore runs with that conversion
disabled, which makes a checkpoint a byte-for-byte record rather than a
normalised one.

The same snapshot answers a different question: what did this turn actually
change. Three records already describe intent and activity, and none of them
describes outcome. The diff preview shows what a reviewed edit meant to do, the
mutation audit records what `write_file` and `edit_file` did, and the execution
audit records that a command ran. Nothing records what a command did to the
files, because `shell` writes nothing to the mutation store, so a formatter, a
`sed`, or a code generator changed the workspace invisibly.

Comparing the working tree against the checkpoint closes that gap, and the
comparison has to be tree against tree. A checkpoint tree is built by staging
everything into a temporary index, so it contains files that are untracked in
the real one. Diffing that tree against the real index therefore reports every
pre-existing untracked file as deleted, with its whole content shown as removed,
because the index has no entry for it. Snapshotting the current worktree the
same way and diffing the two trees compares like with like: a file the user left
lying around before the turn is correctly absent from the result, and a file the
turn created without staging is correctly an addition with real content.

The comparison is anchored to the checkpoint taken before this turn, which the
agent holds, rather than to the newest checkpoint, which is that same one and
would compare the workspace against itself. That anchor is cleared whenever it
would become misleading: a failed capture, a new chat, a session switch, and a
restore.

Sizes are checked before content is read, on both sides, because a diff of a
large file is not shown anyway and reading it first would cost the memory for
nothing. File counts, rendered lines, and line widths all stay bounded, so one
enormous turn cannot stall the interface.

Where git is missing or the workspace is not a repository, checkpoints report
themselves unavailable. There is no weaker fallback, because a fallback that
looked like a checkpoint and covered less would be the same false guarantee this
design exists to avoid.

## Loop and stall detection

An iteration limit is a circuit breaker, not detection. It notices nothing, and
by the time it fires the model has been called `max_iterations` times, every
repeated tool has run, and the turn ends with an error, so the user pays for the
loop and receives nothing.

Two existing mechanisms hide a loop rather than reveal it. Reused tool-call
identifiers are rejected, but that is protocol hygiene against a misbehaving
provider: two calls with different identifiers and identical arguments are
indistinguishable to it. Approval grants are matched by fingerprint, so once the
first repeat is approved for the session, every later repeat is approved
silently. The mechanism that makes ordinary work pleasant is the one that makes
a loop invisible.

Detection compares what actually happened. Each iteration is reduced to a
signature: the tool name and canonical arguments of every call, paired with a
digest of every result. Canonical arguments ignore call identifiers, key order,
and whitespace, so only a genuinely different request counts as different work.

Identical calls returning identical results stall after three iterations,
because a third identical result cannot tell the model anything the second did
not. Identical calls returning changing results are tolerated for twice as long,
since polling a build or a test run is legitimate work that happens to repeat.
The detector errs toward letting real work continue: interrupting genuine
progress is worse than paying for a few extra turns.

The response is to withdraw the tools rather than abort. The next request is
sent with no tool schema and a notice explaining what repeated, so the model
cannot loop and must answer with what it has. A model that returns tool calls
anyway is stopped rather than obeyed, because executing calls that were never
offered is exactly the behaviour being contained. Either way the repeated calls
stay in history, so the transcript still shows what happened.

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

## Rendering untrusted text

Textual treats square brackets in widget content as markup tags, so text the
application did not author cannot be rendered with markup enabled.
A session title, a tool argument, a regular expression, and a validation error
all routinely contain brackets, and a single unbalanced one raises `MarkupError`
from inside `compose`, which takes down the screen rather than showing bad text.

Every widget that renders computed text therefore passes `markup=False`.
Because that is easy to forget on the next widget, a unit test parses each
module under `tui/` and fails on any `Static` or `Label` whose first argument is
not a literal and which does not set `markup`.
The rule is checked structurally rather than by escaping at each call site, so a
new widget cannot quietly reintroduce the crash.

An invalid tool call is treated the same way: arguments that fail validation
become an error result the model reads and retries, so a schema mistake costs a
tool call rather than the turn.

The interface is sized for a terminal that can be any width, so anything that
cannot fit is dropped at a boundary the reader can see rather than cut wherever
the edge happens to fall.
The footer measures the space its shortcut hints actually have and emits whole
hints until they run out, because a hint chopped to `ctrl` is worse than a hint
that is absent.
Widths that must hold a fixed label are sized to that label: a Textual button
reserves two columns beyond its own padding, so a nine-column button silently
renders `Details` as `Detai`.

Shutdown may land at any point in a turn.
The composer can already be gone when the turn settles, so setting the busy
state tolerates a missing widget, and draining the turn worker treats a failed
worker the same as a cancelled one.
Closing the window mid-turn is an ordinary event, not an error.

## Tools

Tool definitions, calls, arguments, and results are typed values.

Arguments cross the model boundary as JSON and are validated before approval or execution.

Approval-required calls are prepared once. Preparation resolves the tool and
parses its arguments. The approval fingerprint, approval display, and eventual
execution all use that same prepared value, so the system never approves one
parse and executes another.

Tools are registered explicitly. Restricted tools, especially filesystem tools, must enforce their own security boundaries.

The shipped filesystem tools share one sensitive-path policy and are rooted at
the canonical project root:

* `read_file` returns bounded UTF-8 line ranges and never exposes paths outside
  the project or known credential locations.
  The range is optional: a bare path reads from line one up to the 500-line cap,
  and `has_more` tells the model when to ask for the next window
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

## Outbound web access

`web_fetch` is the sanctioned network egress path. It matters that it exists
separately from `shell`: the certified sandbox denies network entirely, so a
command cannot reach the internet from there, and smuggling requests through the
shell would put egress outside every control described here.

The address rule is an allowlist, not a blocklist. Only publicly routable
addresses are permitted, which is the same posture as everywhere else in this
system: unknown is refused rather than assumed safe. A blocklist of the usual
private ranges misses carrier-grade NAT, which Python classifies as neither
private nor global, and it misses IPv4 addresses smuggled inside IPv6 through
mapped and 6to4 forms. Requiring `is_global`, unwrapping embedded IPv4, and
naming the classic ranges explicitly covers all three, and the explicit list
keeps the boundary stable when the standard library reclassifies a range.

A URL is refused before any connection when it is not http or https, carries
credentials, or has no host. The host is then resolved and **every** returned
record is validated, not just the one that will be used, so a name that answers
with one public and one private address is refused outright. The connection is
then pinned to a validated address with the original host in the `Host` header
and in TLS SNI, so a name that resolves differently a moment later cannot
redirect the request into the internal network.

Redirects are followed manually rather than by the HTTP client, because each hop
is a fresh target that has to clear the same checks. A public page redirecting
to cloud metadata is the standard way this boundary is defeated, and it fails
here at the second hop rather than the first.

Responses are bounded twice: once on the wire, where reading stops at the byte
limit rather than trusting `Content-Length`, and once on the extracted text.
Only text-like content types are accepted, so the model is never handed a
decoded binary. Preformatted blocks keep their whitespace, because collapsing it
would destroy the code samples that make documentation worth fetching.

Retrieved text is untrusted third-party input. It is returned with an explicit
notice and the prompt guidance says to treat it as data and never as
instructions. That is mitigation, not a guarantee: a model can still be
influenced by what it reads, which is the reason `web_fetch` requires approval
and the reason its content is fenced rather than blended into the transcript.

## Code intelligence

`find_symbol`, `goto_definition`, `find_references`, and `get_diagnostics`
answer questions with a language server instead of a text search, so a name
resolves the way the compiler resolves it. Grep cannot separate a definition
from a mention, or one scope from another; these can.

The stack is layered so each piece is testable alone. Framing is pure: bytes in,
JSON-RPC messages out, tolerant of a message split across arbitrary chunks and
bounded so a corrupt header cannot consume memory. The transport owns the
process, correlates responses by request id, answers server-to-client requests,
drains stderr for diagnosis, and supports start, stop, and restart. The client
owns the handshake, document synchronisation, and the read-only queries. The
manager discovers servers on `PATH`, starts at most one per language on first
use, and remembers a server that failed to start rather than retrying it on
every call.

Positions cross the tool boundary one-based, matching what `read_file` returns
and what a person reads off an editor, and are converted to the protocol's
zero-based positions inside the client.

Real servers are stricter than a specification suggests. Declaring
`workspaceFolders` support in the client capabilities made pyright accept the
handshake and then silently analyse nothing, because that flag promises dynamic
workspace-folder management this client does not implement. The rule is to claim
only capabilities that are actually implemented: an unimplemented promise is
worse than an absent one, because the server changes its behaviour to match it.

This version is read-only by construction. Rename, code actions, formatting, and
workspace edits are absent, so a language server can never change a file behind
the mutation review path.

## Shared JSON-RPC transport

Language servers and MCP servers are the same shape of problem: a child process
speaking JSON-RPC over stdio, with request correlation, server-initiated
requests, notifications, timeouts, and a stderr tail worth keeping when the
process dies.
`jsonrpc/transport.py` owns that, and knows nothing about either protocol.

The one genuine difference is framing. LSP delimits messages with a
`Content-Length` header; MCP delimits them with a newline. So framing is a
parameter, not a branch: `Framing.encode` writes one message and `Framing.reader`
returns a buffer that turns a byte stream back into messages. `lsp/protocol.py`
supplies header framing, `mcp/protocol.py` supplies line framing, and neither
package imports the other.
A newline inside a JSON string never splits a message, because the encoder emits
compact JSON where every literal newline is escaped.

## Model Context Protocol servers

An MCP server is third-party code the user chose to run, and the whole subsystem
is built around that being true.

The client performs the `initialize` handshake, requires the server to state a
protocol version, sends `notifications/initialized`, and refuses to list or call
anything before that completes. A server that never finishes the handshake is
abandoned on a timeout rather than waited on.

Everything the server sends is parsed defensively. A tool listing that is not a
list yields no tools instead of an error; an entry with an unusable name is
skipped; an entry whose schema cannot be bounded is skipped rather than trusted.
The number of tools per server is capped, so a hostile server cannot flood the
model's tool list.

Schema bounding is the security boundary that matters most, because a tool schema
is attacker-controlled text that ends up in a request to the model. Bounding caps
nesting depth, property count, enum size, and description length; keeps only the
JSON Schema keywords TrueCoder actually implements, dropping `$ref`, `allOf`, and
vendor extensions rather than passing them through; and sets
`additionalProperties` to false whatever the server asked for. A schema that
violates a bound removes that one tool, never the server's other tools.

Tool names are namespaced as `mcp__<server>__<tool>` before registration. This is
not cosmetic: without it a server offering `read_file` or `shell` would shadow a
built-in tool whose safety properties the user is relying on. Two servers
offering the same tool name also stay distinct.

The adapter reports `strict: false` on its definitions, because the schema came
from the server rather than from a Pydantic model TrueCoder controls, and
claiming strictness for a schema it did not author would be the same class of lie
as a backend claiming isolation it cannot enforce.
Required fields are still checked before a call leaves the process.

Results are size-capped and returned with a standing note that they are
third-party data. The prompt guidance says the same thing in the same terms as
`web_fetch`: report it, quote it, never follow instructions inside it, and treat
a result that tries to redirect the agent as an attempted attack worth naming.
Non-text content blocks are named but not inlined.

Every server tool requires approval, like every other tool. Nothing about being
configured makes a call pre-authorised, which is the opposite of the hook rule,
because a hook is a command the user wrote and a server tool is a call the model
invented against a schema a third party wrote.

Anything that accepts a workspace-relative path shares one containment rule.
`Path.is_absolute` is platform-dependent and answers the wrong question here: on
Windows `/etc` is not absolute, and on POSIX `C:/Windows` is not absolute either,
so each platform silently accepts the other's rooted paths. Configuration files
are portable, so a path rooted under either convention is refused under both, and
drive-relative forms like `C:Windows` are refused as well. Containment against the
resolved project root still runs afterwards, because a purely relative `../..` is
an escape that no syntactic check can catch.

One server never affects another. Startup records a status per server, and a
server that fails to start, times out, or resolves its working directory outside
the workspace is reported and skipped while the rest of the application starts
normally.

## Reviewable file mutations

`write_file` and `edit_file` implement a `MutatingTool` protocol: given validated
arguments, they can describe the change they intend to make before anything is
approved. The agent asks for that preview while building the approval request,
and the UI renders it as a unified diff with hunk headers, line numbers, and
colored gutters. Reviewing a code change as escaped JSON arguments is not a
review.

A preview is read-only display evidence and never participates in the approval
fingerprint. The fingerprint already covers the canonical arguments, workspace
identity, and execution contract, which together determine the effect; the diff
only describes how that effect reads against the current file. It follows that
a preview failure must never block approval, so the agent absorbs any error from
it and falls back to the raw arguments.

Diffs are bounded like everything else: at most 400 rendered lines, 500
characters per line, and three lines of context per hunk, with a visible
truncation marker when a change exceeds them. The line matcher runs with
`autojunk` disabled, because that heuristic treats lines repeated across a long
file as noise and would stop closing braces and blank lines from aligning.

A change that differs must never render as an unchanged file. Splitting text
into lines discards the terminators, so a CRLF file rewritten with LF content
produces identical line lists and no hunks even though every line on disk
changes. The diff therefore reports a trailing-newline change and a line-ending
change alongside the hunks, and because equal line lists are exactly the
condition that produces no hunks, any difference that survives splitting is
classified as one of the two. That makes an empty diff mean identical text
rather than merely identical line content.

Applied mutations are recorded as durable evidence. Each record carries the
tool, path, mutation kind, SHA-256 of the file before and after, byte counts,
line deltas, and the originating call, turn, session, and workspace. The store
is its own WAL-journaled SQLite database with its own schema version and
insert-only triggers. It is deliberately not a table inside the execution audit:
a file change has no phase machine to arbitrate and no live resource to recover,
and raising the execution audit's schema version would make an existing
installation report an unsupported database and lose shell execution.

Recording is best effort, which is a deliberate divergence from the execution
rule that audit failure withholds the result. An execution can be reported as
failed because its result is still in flight, but `os.replace()` has already
landed by the time a mutation could be recorded, so failing the call would tell
the model the file did not change when it did. A storage failure therefore
increments a counter on the store rather than fabricating a failed outcome.

All shipped filesystem tools require the normal awaited approval interaction.
Successful and failed calls remain part of the current turn and are persisted
with that turn once the model produces its final response. The TUI renders
compact completed summaries such as `Listed src · 4 entries`,
`Matched . · 12 matches`, `Searched src · 3 matches`, and
`Edited src/app.py · 1 replacement`; the same summaries are reconstructed when
a session is resumed.

An edit call carries a list of edits rather than one replacement. They apply in
order to the in-memory text, each seeing the result of the one before it, and the
file is written once at the end. A series that fails at any point writes nothing,
so a partially applied refactor is not a state the workspace can reach. The
rejection names which edit failed and why, because "old_text was not found" is
useless when four were submitted.

This matters for cost as much as correctness. A ten-site change used to be ten
tool calls, ten approvals, and ten model requests against a fixed iteration cap.

## Task planning

The planner exists so a long task keeps its shape. It is a checklist, not an
issue tracker: ordered steps, three statuses, no assignees, priorities, due
dates, dependencies, or backlog.

`truecoder.planning` holds the domain. A `Plan` is a frozen tuple of steps that
validates itself on construction: between one and twenty steps, titles
normalized to a single line of at most 120 characters, and **at most one step in
progress**. That last invariant is what forces the model to sequence its work
instead of marking everything active at once, and it is what makes the plan
legible in one glance. `PlanStore` holds at most one plan for the active task.

`update_plan` replaces the whole list on every call rather than applying
incremental operations. Replacement is idempotent and needs no reconciliation;
incremental edits would require the model to track stable step identifiers
across calls, which is exactly the thing models do unreliably. An invalid plan
is rejected as a recoverable `invalid_plan` tool error and leaves the previous
plan intact, so a bad call costs the model one turn rather than its plan.

The tool is the only tool that does not require approval. It touches nothing
outside memory, and gating it would put an approval prompt between the agent and
every checklist update.

The plan is deliberately not persisted. It is scratch for the task in flight,
cleared by a new chat or a session switch. Because the store does not survive a
restart, the UI also skips historical `update_plan` calls when redrawing a
resumed session: showing tool cards for a plan the model no longer holds would
imply state that does not exist.

The UI mounts one `PlanCard` at the point of the first successful call and
updates it in place afterwards, mirroring the one-evolving-card treatment
already used for shell executions. This is driven directly from the agent event
stream rather than through a published event. The transcript is an ordered log,
and a queued message would be processed by the app's message pump while the
turn worker keeps advancing, which would let a plan update land at the wrong
position relative to the text and tool cards around it.

## Approval

Approval is an awaited request-response interaction.

A card is moved into its approval state by the approval handler alone, and the
pending decision is registered before that happens. The agent emits an
approval-requested event immediately before awaiting the handler, so acting on
that event as well would arm the buttons while nothing was yet listening, and
`_resolve_pending_approval` discards a decision that arrives with no pending
request. The gap is normally sub-millisecond, which is precisely why it should
not be left to timing: a loaded machine widens it, and the lost click leaves the
turn waiting on an approval that can never arrive.

The approval service owns reusable grants. The UI is only an injected
request-response handler: it presents the exact request, returns the selected
decision and scope, and does not decide whether an earlier grant matches.

Approval policy belongs to the tool. Orchestration belongs to the agent. Presentation belongs to the UI.

Every request contains:

* canonical validated arguments
* the canonical workspace identity
* a versioned SHA-256 fingerprint of all security-relevant inputs
* the scopes that policy allows the UI to offer
* for shell execution, the effective request, limits, selected backend,
  capability levels, risk, policy version, and reasons

Transient call, execution, session, and turn IDs are not part of a reusable
operation fingerprint. The session ID still scopes a session grant, and the
workspace ID scopes a workspace grant. Argument, workspace, limit, capability,
backend, policy, or risk changes produce a different fingerprint and therefore
require another approval.

The available scopes are:

* `once`, which is never stored
* `session`, which matches only the same session and exact fingerprint
* `workspace`, which matches only the same workspace and exact fingerprint

Reusable grants are currently held in memory for the running application. They
are never stored in the repository and do not survive an application restart.
Rejections are never remembered.

The safe-scope calculation is authoritative. Shell-script mode and any
execution above low risk permit `once` only. A model-facing tool named `shell`
also defaults to `once` only when execution details are not available. The
approval service rejects a handler response that selects a scope outside the
request's allowed set, so a hidden or buggy UI control cannot turn an arbitrary
shell command into a reusable grant.

## Execution control plane

The execution package defines the shared control-plane contracts before any
platform backend starts processes.

An `ExecutionContext` correlates one execution with its tool call, active
session, active turn, canonical project root, stable workspace identity, launch
time, and opaque execution ID. The workspace identity is a versioned hash of
the canonical host path, with host case normalization where required.

`ExecutionContextFactory` validates all runtime identities and canonicalizes the
project root. The active turn receives its own ID when it begins and loses it
when the turn completes, aborts, or resets.

Cancellation uses a source/token split:

* the service owns the `CancellationSource`
* backends receive the read-only `CancellationToken`
* the first cancellation request records the reason and releases every waiter
* later requests are idempotent and cannot replace the original reason

`ExecutionRegistry` maps opaque execution IDs to the exact active entry. It
supports lookup and cancellation by execution ID. Registration rejects
duplicates, and cleanup requires the same entry object that was registered, so
stale cleanup cannot remove a newer execution that happens to use the same ID.
`ExecutionService` is the public owner of registration, lookup, cancellation,
and unregister operations.

This layer does not itself execute or sandbox commands. It supplies identities
and cancellation authority to the selected platform backend.

## Pure execution components

Policy, child-environment construction, and output processing are deterministic
components with no operating-system calls. They consume explicit platform and
request data, which lets every future backend use the same decisions and makes
their behavior testable without starting a process.

The policy evaluator applies ordered rules to the validated execution request.
It distinguishes known read-only, test, build, package, network, deletion,
permission, Git, shell-script, and unknown commands. A policy decision contains
the effective limits, risk level, stable structured reasons, approval
requirement, and exact capability requirements. Requested limits can only be
tightened against policy ceilings. Policy classification improves safety and
approval UX, but it is not an isolation boundary.

Versioned trusted-command rules are applied after that base classification and
before backend selection. They match only structured exec requests by portable
bare executable name. A matching rule may impose an approval requirement or a
maximum accepted risk; exceeding that ceiling denies the request. It can never
remove an approval already required by the base policy, lower the classified
risk, widen a capability, or increase a limit. An invalid rules file makes
execution unavailable at startup instead of being ignored.

The environment builder never copies the complete parent environment. It starts
from a small platform-specific allowlist, optionally includes explicitly
configured names, then applies TrueCoder-defined and request-specific values in
a fixed override order. Environment names use POSIX case-sensitive or Windows
case-insensitive comparison as selected by the caller. Names matching known
credential, cloud, token, password, private-key, or secret rules are removed.
An explicitly requested sensitive name is also reported as a policy violation.
Metadata records names and removal reasons only; values are excluded from
representations and can be rendered only as `<redacted>`.

Output processing treats produced, retained, and returned output as separate
quantities. `BoundedByteStream` counts and hashes every raw byte while keeping
only fixed-size first and last windows. `OutputCollector` drains stdout and
stderr independently, incrementally decodes UTF-8, removes terminal control
sequences, redacts configured secret values even when a match crosses chunk
boundaries, and emits one signal when the combined production limit is crossed.
Final stdout and stderr share one return-byte budget, use explicit truncation
markers, and remain bounded regardless of total process output.

The durable audit collector reuses the same bounded byte primitive, so audit and
runtime evidence agree on complete byte counts, SHA-256 digests, sanitization,
and truncation. Deterministic property-style tests vary chunk boundaries,
Unicode splits, secret-name casing, command inputs, and limit values. The core
invariant is that arbitrary chunking produces the same bounded result while
memory grows only with configured retention and redaction bounds.

## Backend protocol and discovery

The backend protocol defines one lifecycle shared by every process adapter.
`ExecutionBackend.start()` accepts the immutable
request, execution context, read-only cancellation token, and an awaited
resource-registration callback. A backend may acquire a native process group,
Job Object, or container before invoking that callback, but project-controlled
code must remain behind a launch gate until the exact
`BackendResourceIdentifier` is durably attached to the pending audit run. If
registration fails, the backend cleans the partial resource and never releases
the gate. A successful start returns an `ExecutionHandle` with the matching
execution ID, registered resource, single-owner raw output iterator, stable
wait semantics, idempotent termination, and idempotent cleanup.

Cleanup ownership transfers exactly once:

1. During `start()`, the backend owns every partially acquired native resource.
2. The backend registers the exact resource while user code remains gated.
3. A failed acquisition or registration cleans partial resources before raising.
4. A successful return transfers all native ownership to the handle.
5. The execution service owns that handle's lifecycle and must eventually call
   cleanup after normal exit, termination, cancellation, or monitoring failure.

Termination, waiting, and cleanup are separate operations. Termination asks the
complete native resource to stop. Waiting observes and reaps its terminal state.
Cleanup releases pipes, handles, temporary state, and other remaining
resources. Repeating any of these operations must not target a guessed or reused
native identifier. One reusable backend contract suite encodes these invariants
and is applied to the fake, POSIX, Windows, and container adapters.

Discovery happens through an injected `DiscoveryIO` boundary. Unit tests can
model Linux, macOS, Windows, and unknown hosts without depending on the CI
runner. `SystemDiscoveryIO` is the only discovery component that inspects the
actual machine. It detects:

* normalized operating-system family, architecture, and release
* canonical installed POSIX, PowerShell, and Windows command-shell paths
* Linux cgroup v2 mount, available and enabled controllers, exact delegated
  path, and delegated writability facts
* Docker, Podman, and nerdctl client presence and versions
* container service reachability and server versions
* rootless mode as `yes`, `no`, or `unknown`

Version and runtime probes use fixed executable argument vectors rather than a
shell. They have short timeouts, bounded combined output, a minimal child
environment, terminal sanitization, and explicit failure statuses. Discovery
never pulls an image or runs project-controlled code. Client installation is
not confused with service reachability, and a mounted cgroup filesystem is not
confused with writable delegated enforcement.

The resulting `DiscoverySnapshot` always describes POSIX, Windows, and
container candidates. Backend capabilities are derived from measured host
facts plus conservative backend knowledge; they are not optimistic class
constants. An unavailable backend contains stable structured reasons. An
available container descriptor identifies the exact inspected runtime.

Capability matching is pure. It compares every field in the shared
`CapabilityRequirements` independently against a discovered descriptor:

```text
required none        accepts unsupported, best_effort, or enforced
required best_effort accepts best_effort or enforced
required enforced    accepts enforced only
```

Matching also checks execution mode, filesystem mode, and explicit or resolved
shell support. It returns every mismatch instead of stopping at the first.
Selection is deterministic and never mutates the request, effective limits, or
requirements. `local` permits only the current host's local backend;
`container` permits only the container backend; `auto` may move to another
candidate only when that candidate satisfies the complete unchanged contract.
An explicit shell is never silently substituted. If selection fails, the error
preserves the reasons for every permitted candidate, and states them in its
message rather than only carrying them as data.
A request nothing can satisfy is the model's to fix, so the shell tool reports it
as `no_compatible_backend` naming the backend that refused and why, instead of
folding it into the generic infrastructure error.

The local and container backends support disjoint filesystem modes, which makes
the shell tool's defaults decide the backend before `auto` ever ranks candidates.
Local backends support only `filesystem_mode="host"` and report network isolation
as unsupported; the container supports only the workspace modes. A default of
`workspace-read` or `network_access=false` therefore eliminates every local
candidate, and every command lands in a sandbox that holds no project
dependencies and no package index.
The shell tool consequently defaults to `filesystem_mode="host"` and
`network_access=true`, so ordinary work reaches the machine the user is actually
on, and asking for the container, a workspace filesystem mode, or no network is
what opts into isolation.
The approval gate, not the sandbox, is the boundary that protects the default
path: every command is fingerprinted, previewed, and approved before it runs.

Policy keeps one lever over that default. A command whose risk reaches critical
and that policy still permits has its filesystem and network isolation raised to
the configured minimum, which no local backend can provide.
Policy never rewrites the request to achieve this, because selection never
mutates what it was asked for. It raises the requirement and lets selection fail,
so a critical command asking for the host is refused with both rejections named
rather than quietly run unprotected. Moving it into the sandbox is an explicit
new request.
Every rule that reaches critical today is also denied outright, so the escalation
matters most for `unknown_risk`: an operator who sets it to `critical` gets every
unrecognised command forced into the sandbox instead of onto their machine.

`BackendDescriptor.available` means the host prerequisites for that adapter
were discovered. It does not by itself authorize a command. Every concrete
backend must pass the shared contract suite before the execution service can
register it.

## POSIX local backend

The POSIX backend is a trusted local process backend for Linux and macOS. It
provides reliable process lifecycle management, not filesystem or network
isolation. Its descriptor supports only `filesystem_mode="host"` and continues
to report filesystem and network isolation as unsupported.

Launch planning is pure. Exec mode preserves the original argv tuple exactly.
Shell mode resolves one canonical shell path from discovery and passes the
complete script as one argument. The child environment is built by the shared
environment component before native resources are acquired. Sensitive
requested variables fail before process creation, while inherited secrets are
removed.

Each execution uses a small trusted supervisor:

```text
TrueCoder
  └─ supervisor (new POSIX session)
       └─ blocked project leader (new process group)
            ├─ child
            └─ grandchild
```

The supervisor forks the project leader before reporting readiness, but that
leader waits on a private launch pipe and has not executed project code. This
makes the project PGID available for the durable
`BackendResourceIdentifier`. TrueCoder awaits the audit resource-registration
callback and sends `START` only after it commits. Failed registration,
cancellation, malformed protocol data, or parent lifetime-pipe EOF kills the
blocked group without executing the requested command.

Parent and supervisor communicate with bounded, versioned, length-prefixed JSON
frames. Command text and environment values travel through the private config
pipe rather than process arguments. Structured exec never uses a shell.
Supervisor error frames distinguish failed process setup or `execve` from a
normal command exit.

The returned handle starts one stdout pump and one stderr pump. Each emits raw
chunks into a bounded queue and preserves order within its own stream. The
backend does not decode, redact, hash, sanitize, or retain output; those remain
the shared output collector's responsibility. Only one consumer may claim the
iterator.

`wait()`, `terminate()`, and `cleanup()` each cache their first task. Repeated or
concurrent calls therefore share one terminal observation and one cleanup
operation. The first termination reason wins. Normal termination asks the
supervisor to stop the complete project group, waits for the requested grace
period, then escalates to `SIGKILL`. The lifetime pipe gives the supervisor the
same cleanup responsibility if TrueCoder disappears.

Linux hard limits use only controllers that discovery found both available and
enabled in the writable delegated subtree. Each execution receives a
token-derived child cgroup. `memory.max` and `pids.max` apply hard tree-wide
limits, while cgroup CPU accounting allows the supervisor to detect total
execution CPU consumption. Missing controllers remain explicitly best effort
and use POSIX rlimits where available. macOS uses the same lifecycle contract
with best-effort rlimits and no cgroup claims.

Recovery never trusts a PID alone. The durable identity includes the supervisor
PID, project PGID, host identity, Linux boot ID and process start ticks,
protocol version, ownership token, and optional owned cgroup path. Linux
recovery signals only a complete exact match. An absent supervisor is recorded
as absent. A live macOS resource fails closed after restart when exact ownership
cannot be proven with the available facts.

## Durable execution audit

The audit subsystem is the evidence boundary for shell execution. An
execution must first obtain an `AuditRunHandle` from `AuditService.admit()`.
That handle is returned only after the pending run and its first event have
committed to SQLite. If permissions, schema verification, database access, or
the write fails, admission raises and no backend is authorized to start.

Audit storage is separate from conversation sessions and lives in the operating
system's user-data directory. The versioned SQLite schema contains:

* one run row with execution, tool-call, session, turn, and workspace identities
* an immutable ordered event log
* at most one exact backend resource identifier
* one atomic terminal finalization
* SHA-256 output digests, byte counts, and bounded first/last previews

Run state is a strict `pending → running → terminal` state machine. Policy
denial, approval rejection, failed start, normal success, nonzero exit,
timeout, cancellation, limit termination, cleanup failure, and recovery each
use explicit terminal outcomes. A nonzero command exit is evidence, not an
audit infrastructure exception.

SQLite uses WAL journaling, full synchronous durability, foreign keys, an
immediate transaction for every state change, and triggers that reject edits or
deletions of events, resources, and terminal runs. Finalization inserts the one
terminal event and updates the run in the same transaction. Retrying the exact
same finalization is idempotent; a conflicting terminal result is rejected.

The database directory and files are private by construction. POSIX uses
directory mode `0700` and file mode `0600`, including SQLite sidecars. Windows
removes inherited ACLs and grants full access only to the current user and
LocalSystem. Failure to establish those restrictions makes audit storage
unavailable rather than silently weakening it.

`BoundedOutputEvidence` hashes every stdout and stderr byte while retaining only
a fixed-size preview. When output is larger than the preview budget, it keeps
both the beginning and end with a truncation marker. This preserves the final
traceback without allowing audit memory or rows to grow with unbounded process
output. Environment variable names may be summarized, but their values are
never copied into audit metadata.

Startup recovery leases every nonterminal row before acting on it. A recovery
handler receives the exact persisted backend name, resource kind, native
identifier, ownership token, host identity, and native details; it never
searches for a process by a guessed or reused PID. Pending runs with no resource
close as never started. Exact resources close as absent, terminated, or
recovery failed. Every recovery path attempts one normal terminal
finalization. Concrete POSIX, Windows Job Object, and container recovery
handlers are registered during startup only when their host or runtime
prerequisites are present.

The store also exposes workspace-scoped, newest-first run queries with a hard
row limit for the audit viewer. It never performs an unbounded history read.
After recovery, startup applies the configured retention policy. Operational
retention always keeps every nonterminal row and removes expired terminal
evidence by building and validating a replacement database, then atomically
installing it. This preserves the schema, foreign keys, immutability triggers,
and recent evidence without deleting trigger-protected terminal rows in place.
A retention failure leaves shell execution unavailable.

## Execution orchestration

`ExecutionRunner` owns one execution from durable admission to one normalized
result.
It walks a fixed order: admission, policy, exact preparation, approval, active
registration, resource-gated backend start, supervision, termination, drain,
cleanup, and one durable finalization.

`prepare_execution` resolves everything a backend would otherwise recompute.
A `PreparedExecution` carries the effective request, the selected backend
descriptor, the constructed child environment, and the resolved shell, so no
backend re-derives environment, shell, limits, or capabilities.
`BackendRegistry.get_exact` then returns the live backend only when its current
descriptor still equals the descriptor that preparation selected; any drift is
an explicit unavailable error rather than a substitution.

Lifecycle state is separate from the transient event vocabulary.
`LifecycleState` validates every internal transition and rejects backwards or
skipped moves, including the pre-start paths into finalization used by policy
denial, approval rejection, and a starting event that cannot be stored.

Terminal outcomes are arbitrated, never inferred from task scheduling.
The runner creates one output task and four watchers for backend exit,
cancellation, timeout, and the output limit.
Because `asyncio.wait` can return several completed tasks at once, every
completed signal becomes a candidate claim and `resolve_terminal_claim` ranks
them by a fixed priority: natural backend exit, output limit, resource limit,
cancellation, then timeout.
A command that exits at its deadline is therefore never labelled as timed out,
and an enforced limit is never rewritten as a generic timeout.
`TerminalArbiter` records the winner once; later claims, including a late
cancellation, receive the original claim and cannot replace it.

The output task is the only owner of the backend output iterator.
It reads each raw byte once and accounts for it twice: the collector hashes and
counts the exact bytes for audit evidence, and the same update supplies bounded
sanitized text for the interface.
Digests always cover the raw stream, never the sanitized or redacted preview.
After a winner exists the runner cancels the watchers it no longer needs but
keeps draining output, so a backend pipe cannot deadlock behind a full buffer.

Two different deadlines apply during a run.
The request timeout is a user-visible execution outcome.
A short internal safety deadline, used while waiting for a broken backend to
finish terminating or to close its pipes, is an infrastructure failure and is
reported as one.
Output that cannot reach EOF within that deadline marks the audit evidence
incomplete instead of claiming complete evidence.

Every audit write has a defined failure rule, and audit is a mandatory
dependency rather than best-effort logging.
Admission failure refuses the run before any selection, approval, registration,
or backend call.
A pre-start event failure never starts the backend and attempts one
infrastructure finalization only while the store remains usable.
A durable attachment failure leaves the backend launch gate closed, so no
unrecorded project process can run.
A mark-running failure terminates and cleans the returned handle while the
exact resource stays attached for startup recovery.
Losing a runtime event is fatal to the run rather than something to continue
past.
A finalization failure withholds the result entirely and leaves a nonterminal
row for recovery; no terminal row is ever faked in memory.

Only one method calls `audit.finalize`, and it receives an immutable
`TerminalMaterial`.
The pure functions in `results.py` convert that material into an
`AuditFinalization` and convert the persisted record into the public
`ExecutionResult`, including its exact audit ID. The public result also carries
a bounded stable reason code and user-actionable reason message for policy
denial, approval rejection, selection failure, and vetted startup failure.
Arbitrary native diagnostics are not copied into the model-visible result.
Durations come from injected monotonic time; audit timestamps remain UTC wall
time.
Incomplete cleanup is never reported as success: the row records
`cleanup_failed` over the real command outcome and the caller receives a typed
cleanup error.

Lifecycle events are transient and bounded.
`LifecyclePublisher` assigns dense sequence numbers, keeps a reserved slot so
the single terminal event survives a saturated buffer, drops the oldest
transient events under pressure, and never lets a slow or failing sink block or
fail a run.

`ExecutionService.execute` owns the whole request-to-result lifecycle.
It evaluates policy, selects a backend from the discovery snapshot, prepares
the exact launch, and then calls the runner.
A policy denial and an unavailable backend both still reach one durable
terminal row, so no route escapes audit.
`run_prepared` remains the lower-level entry point for callers that already
hold a prepared execution and a decision.

Cancellation remains a request rather than a terminal event.
The active control entry is registered immediately after durable admission and
before approval is awaited, so an execution is addressable by ID for its whole
life rather than only once it reaches the backend.
The runner checks the token after every awaited pre-start boundary.
A cancellation that lands before the command starts records `failed_to_start`
with a `cancelled_before_start` detail, because the command genuinely never
ran, while the caller receives a `cancelled` result carrying the reason.
The service signals the token first and only then records a durable
cancellation-request event, so cancellation never depends on a successful
write.
The active entry carries the audit handle for that routing instead of a global
side map.
Repeat requests report `ALREADY_REQUESTED`, unknown IDs report `NOT_FOUND`, and
a cancellation arriving after a natural exit has already won stays a harmless
request outcome.

## Container sandbox

The container backend is an adapter behind the same `ExecutionBackend`
contract, not a second execution service.
It performs no policy, approval, or service registration of its own.

The container subsystem certifies exactly one profile: Linux, the Docker
dialect, and one pinned execution image.
Podman and nerdctl are refused by the plan and by capability derivation until
their dialects pass the same tests, and non-Linux hosts report
`container-platform-unsupported`.

`BackendStartContext` carries the durable audit run ID into a backend without
giving backends access to the audit service, so container labels can record the
exact run they belong to.

Launch planning is pure.
`build_container_plan` converts the prepared execution into typed mounts,
labels, limits, and argv; the Docker dialect renders that plan into exact
argv, and unit tests assert both the required flags and the absence of every
forbidden one.
The plan refuses the host filesystem mode, refuses network access without a
configured isolated network, clamps requested limits to the configured
ceiling, and rejects a workspace the non-root sandbox user cannot read.
Because the image runs as a fixed non-root UID, `workspace-write` is refused
up front when the host workspace does not grant that user write access, rather
than producing a container that silently cannot write.

The image is pinned by content digest in `container/image.lock` and launch uses
`--pull never`, so a reachable daemon without the exact local image leaves the
sandbox unavailable.
Capability derivation comes from verified `ContainerBackendFacts` rather than
optimistic constants, and total CPU seconds are advertised as best-effort:
the trusted entrypoint monitors aggregate cgroup CPU accounting and the
adversarial sandbox suite verifies that a busy process is terminated at the
requested budget, but the backend still refuses to advertise that userspace
monitor as kernel-enforced isolation.

Start uses create, register, then start.
The container is created stopped, inspected for exact state, labels, and image
digest, and only then offered to the durable registrar; project code cannot run
until attachment commits.
A registrar failure force-removes the stopped container, and every partial path
removes the private environment file and scratch directory.
The handle owns one exact container: a single output owner, cached
wait/terminate/cleanup, stop-then-kill escalation by full immutable ID, explicit
removal, and absence verification.

The trusted entrypoint launches the requested command in its own process group.
Signal forwarding and CPU-limit termination target that group, not the
entrypoint's own group, so supervision cannot accidentally signal itself before
the project descendants have been cleaned up.

Proven adversarially against real Docker: a host secret outside the workspace
is unreadable, `workspace-read` cannot mutate the host tree, the root filesystem
is read-only with only approved tmpfs writable, network denial blocks a real
external canary, no runtime socket is visible, every capability is dropped with
no-new-privileges active, exceeding memory normalizes to `memory_limit`, streams
stay separate and raw, PID growth is bounded, a CPU-bound process is terminated
at its aggregate budget, and no container, client, or temporary file leaks.

Container discovery does not trust a matching image name. It inspects the exact
locally available digest and verifies its platform, non-root user, and
TrueCoder entrypoint labels against `container/image.lock`. A missing or
mismatched image keeps the container backend unavailable; startup never pulls
or substitutes another image.

Container recovery receives the complete immutable container ID stored in the
audit row and rechecks the management, audit-run, execution, ownership, host,
and protocol labels before removing anything. An absent exact container is
safe to finalize as absent. A shortened ID, label mismatch, foreign host, or
failed absence check fails closed and leaves evidence for investigation.

## Shell tool and startup composition

`tools/builtin/shell.py` is the model boundary, not an executor. Its Pydantic
schema defaults to structured `argv` execution and makes shell-script mode an
explicit choice for pipelines, redirects, chaining, expansion, and other real
shell syntax. Exec and shell inputs are mutually exclusive. Working
directories are workspace-relative, canonicalized, required to exist, and
rejected if an absolute path or symlink escapes the project root.

The adapter performs only four operations:

1. Convert validated model arguments into an `ExecutionRequest`.
2. Reuse the exact `ExecutionContext` and `CancellationSource` supplied by the
   active agent tool call.
3. Await `ExecutionService.execute()` exactly once.
4. Format the bounded `ExecutionResult` into a stable JSON tool payload.

It contains no subprocess, Docker, policy, approval, environment, output,
audit, or TUI logic. Requested time, output, memory, CPU, and process limits can
only tighten the configured defaults. Policy tightens them against the
administrator ceiling again before backend selection.

Every model-requested tool call receives a `ToolInvocationContext` created by
the agent. It binds a fresh execution ID to the provider's exact tool-call ID,
the pending turn ID, active session ID, canonical workspace identity, and
project root. The same invocation owns one cancellation source. If the outer
agent task is cancelled, the agent signals that source, shields the tool task
from abrupt cancellation, and waits for execution termination, cleanup, and
audit finalization before propagating cancellation. The interrupted turn is
then aborted without persisting an unmatched tool call.

Shell uses the shared approval service only inside execution orchestration,
after policy and backend selection have produced the effective security
contract. Its generic tool approval flag is therefore `NOT_REQUIRED`; asking
again at the outer tool layer would approve different, incomplete data.
Execution approval carries the selected backend, capabilities, effective
limits, risk, reasons, working directory, command, session, and workspace.
Shell requests permit approve-once only.

All ordinary execution outcomes are successful tool payloads. That includes
exit zero, normal nonzero exits, policy denial, approval rejection, timeout,
cancellation, limit termination, and backend unavailability. The payload keeps
status, exit code, duration, separate bounded stdout and stderr, raw byte
counts, truncation flags, termination reason, backend, and audit ID. Audit or
cleanup failures are different: they become a sanitized infrastructure tool
error because no trustworthy public result exists. Refused and failed-start
results also include a stable reason code and a bounded actionable message, so
the model can change its request without seeing unsafe native diagnostics.

`execution/bootstrap.py` is the composition root for this subsystem. Startup
proceeds in this order:

```text
open and secure audit storage
→ load strict trusted-command rules
→ discover host, shells, cgroups, runtimes, and pinned image
→ construct only exact implemented backend instances
→ recover every nonterminal audit resource
→ compact expired terminal evidence
→ build registry, approval gate, runner, and service
→ publish a health report
→ register ShellTool only when the report is healthy
```

Audit failure, discovery failure, recovery failure, disabled execution, or no
registered backend leaves `shell` absent from the model schema. POSIX is
registered only on a supported POSIX host. Windows is registered only on an
actual Windows host after `WindowsBackend.from_snapshot()` succeeds. Container
is registered only for the certified Linux Docker profile with the verified
pinned image.

`execution/configuration.py` is an optional operator boundary, not a setup step
for ordinary users. With no file, `load_execution_config()` returns safe
zero-configuration defaults. If `<user config>/truecoder/execution.json`
exists, its versioned strict schema can change deployment ceilings, environment
inheritance, audit and lock paths, container defaults, an isolated container
network, retention days, and the trusted-rules path. Unknown fields, invalid
types, and malformed paths fail closed. A model request may still tighten the
effective values but cannot exceed these configured ceilings. Network-enabled
container requests are rejected during selection unless an isolated network
was configured; they never silently inherit the host network.

Shell-specific system guidance is also conditional. The model is told to
prefer argv, request only necessary capabilities, use relative working
directories, and inspect nonzero exits only after the tool has actually been
registered. Direct `Agent` users can initialize lazily on the first valid
turn; the Textual application initializes execution during mount so its first
model request has the final tool schema and prompt.

## Execution presentation

Lifecycle events and bounded output previews reach the interface through two
injected sinks. `ExecutionBootstrapConfig` carries them to `ExecutionRunner`,
and the Textual application forwards each one into its message loop with
`post_message`, which never blocks and never raises into the caller. A slow or
unmounted interface therefore cannot delay or fail a run.

`PreviewSink.publish_bounded` receives the execution id and stream name with
every update. One sink serves every execution, so without that identity the
interface could not route output to the right card.

`tui/execution_view.py` holds the presentation logic as ordinary functions and
values, with no Textual import. It maps each of the fifteen lifecycle stages to
a state, label, and glyph, refusing an unknown stage instead of guessing. A
timeout is never shown as a plain failure, a policy denial is never shown as a
crash, and `failed_to_start` reads as never started.

The approval view is compact by default: command, directory, backend, access,
limits, risk, and the scopes policy actually permits. Access states the
filesystem mode and network decision in one line. Limits list only the limits
that were requested. The complete twenty-five field capability contract remains
available behind the details expander, so the compact view never becomes the
only record of what was approved.

`BoundedPreview` retains a fixed tail of streamed output. The runner already
bounds produced and returned bytes; this bounds what the interface keeps, so a
long-running command cannot grow the widget tree without limit. The rendered
tail never exceeds its configured line count, including the incomplete final
line, and a trim marker states that earlier output was dropped.

`ExecutionCard` renders one execution and decides nothing. It applies typed
stages, appends bounded preview text, and records the audit id on completion.
The initial `requested` lifecycle event carries a bounded display form of the
real argv or script, so the card shows the requested command immediately
instead of a generic placeholder.
Because it can be updated at any lifecycle point, including while the
application unmounts and its children are already gone, a missing child is not
an error for it.

Cancellation is addressed by execution id, not by cancelling the turn. Stop
resolves an open approval as a rejection first, then cancels active executions
by id, and only cancels the worker when nothing narrower is running. A second
cancellation request for the same execution is not sent twice.

Shutdown is the strongest guarantee in this layer. An approval that is still
awaited is resolved as a rejection so its tool task cannot block forever, every
active execution is cancelled by id, and the worker is given a bounded window to
terminate, clean up, and finalize. The interface can disappear at any lifecycle
point without leaving a process or a nonterminal audit row.

Two workspace-scoped operational screens expose the subsystem without turning
the main composer into a control panel. `ctrl+a` opens the audit viewer, which
loads at most 200 recent runs and filters them by text, durable outcome,
backend, and recency; selecting a row shows bounded previews, cleanup status,
reason, and lifecycle events. `ctrl+e` opens the execution health report,
showing audit readiness, recovery readiness, registered backends, and explicit
unavailable reasons. When shell is unavailable, the status bar and startup
notification surface that fact instead of leaving the missing tool unexplained.

## Platform differences

`backends/posix_platform.py` states what each POSIX platform can actually
enforce, rather than letting a shared code path imply a guarantee it cannot
deliver.

Linux has cgroup v2, a boot id, and process start ticks, so it can apply hard
memory and process limits in a delegated subtree and can prove resource
ownership after a restart.

macOS has none of those. `RLIMIT_NPROC` on macOS is per-user rather than
per-process-tree, so applying it as a tree limit could exhaust the whole login
session instead of the command. macOS therefore never applies that rlimit and
reports `process_limits` as `unsupported` rather than `best_effort`. Its
unsupported guarantees are enumerated explicitly: cgroup controllers
unavailable, process limit is per-user, and resource ownership unprovable after
restart.

Neither platform claims filesystem or network isolation, and both support only
the host filesystem mode. The container backend remains gated to the certified
Linux Docker profile, so a non-Linux host reports
`container-platform-unsupported` before any process starts.

## Windows Job Object backend

The Windows backend owns a process tree through a Job Object rather than a
process group. `windows_plan.py` is pure: it normalizes Win32 start errors and
NTSTATUS exit codes into stable reasons, builds `CommandLineToArgvW`-compatible
command lines, and renders non-interactive PowerShell and command-shell
argument vectors. Quoting is verified by round-tripping through a reference
splitter, including embedded quotes, trailing backslashes, and empty arguments.

`windows_native.py` is the ctypes boundary. A process is created suspended, so
the launch gate is the suspended primary thread: the job is created and
configured, the process is assigned to it, the exact resource identity is
offered to the durable registrar, and only a committed attachment releases
`ResumeThread`. A failed assignment or registration terminates and closes every
partial handle before raising.

Job limits are derived from the same shared `ExecutionLimits`. Only requested
limits set their flag. `KILL_ON_JOB_CLOSE` is always requested so closing the
job handle cannot orphan descendants, and processes never break away from their
job.

Recovery requires the full persisted identity: backend, resource kind, host,
protocol version, and a numeric job handle. A job with no active processes is
finalized as absent; anything else fails closed. The host identity includes the
owning TrueCoder process, because a Win32 handle value is not a durable
cross-process identity. After a TrueCoder restart, recovery therefore refuses
to reuse the old numeric handle. `KILL_ON_JOB_CLOSE` is responsible for
terminating descendants when the original process disappears, while audit
recovery records uncertainty instead of guessing.

The shared backend contract and the integration suite run natively on
`windows-latest` in CI. The contract launches real PowerShell processes through
the Job Object backend and covers resource registration, output ownership,
normal and nonzero exits, termination, cancellation, stable waiting, cleanup,
and failed-start cleanup. Windows-only integration probes additionally verify
that Job termination prevents a grandchild from surviving, the active-process
limit blocks descendant creation, and a real timeout reaches one durable audit
finalization through the complete service. The Windows support claim is based
on native boundary tests, not only on pure quoting and planning tests.

## Policy hardening and operations

`trusted_rules.py` holds versioned, user-editable trusted-command rules in the
operating system's user-config directory, written atomically at mode `0600`.
Parsing is strict: unknown fields, unknown risk levels, duplicate rule ids,
duplicate executables, paths in place of bare program names, whitespace or null
bytes in identifiers, and oversized documents are all refused rather than
ignored.

A rule can only tighten. It never changes the classified risk, cannot approve a
denied base decision, and cannot remove an approval the base policy required.
`require_approval=true` may add approval for a matching structured executable.
If the classified risk exceeds `max_risk`, the request is denied before backend
selection. Shell scripts do not match trusted rules because interpreting
arbitrary shell syntax is not a trustworthy executable identity.

Bootstrap loads the rule set once and supplies it to `ExecutionService`.
Every live execution applies it after base policy evaluation. Missing files
mean an empty rule set; malformed or unreadable files fail closed and appear in
execution health as `trusted_rules_invalid`.

`audit/retention.py` calculates the UTC cutoff and a bounded deletion plan.
Nonterminal rows are retained because they are exactly the evidence a stuck or
crashed run leaves behind, and operational configuration cannot disable that
guarantee. `SQLiteAuditStore.apply_retention()` performs the actual atomic
database rebuild after recovery at startup.

`tui/audit_view.py` filters audit rows by outcome, backend, recency, and search
text, orders them newest first, and bounds both the row count and every rendered
value. Detail names matching credential, token, password, or environment-value
rules render as `<redacted>`. Incomplete cleanup is surfaced ahead of the exit
code, because a run that could not clean up is not a successful run.

The viewer is reachable through `ctrl+a`; `escape` closes it. Execution health
is reachable through `ctrl+e`. Both screens are presentation-only consumers of
bounded service data and never mutate audit evidence or backend state.

Parsers and sanitizers are fuzzed deterministically. A fixed seed drives noisy
Unicode, control-sequence, and structural input through the terminal sanitizer,
the bounded byte stream, the bounded preview, the trusted-rules parser, and the
Windows quoter. The invariants are that arbitrary chunking produces identical
digests and previews, that sanitized text never carries escape or null bytes,
that the preview stays within its configured bounds, and that quoting always
round-trips.

## Proving the composed system works

Component tests answer whether a part is correct. They cannot answer whether the
assembled agent does the user's job, and that gap is where this project's worst
defects have lived: a shell tool whose defaults routed every command into an empty
sandbox, a `read_file` whose required arguments the model kept omitting, a context
budget smaller than one ordinary file so every large read told the model to read
again. Each was found by watching a real session fail, and none was reachable by
any component test, because every component was behaving exactly as specified.

`tests/e2e` closes that gap. A scripted model drives a real `Agent` with real
tools against a real temporary workspace, and the assertions are about outcomes
rather than calls: the bytes on disk after a write, the backend a command
actually ran on, whether a result reached the model whole or shortened, whether
the turn finished at all. A rejected write is asserted by reading the file back,
not by inspecting an approval object.

The rule this suite encodes is that a behaviour nobody exercises end to end is a
behaviour nobody has checked, however many unit tests surround its parts.

It has already earned that. The suite writes into a temporary workspace and
deletes it afterwards, which on Windows fails while any handle is still open, and
that is how an unclosed mutation-audit connection surfaced. `Agent.close` released
the memory store and every tool exposing `aclose`, but the mutation audit was
constructed as a local in composition and handed to two tools, so nothing owned
closing it. Shutdown now releases it beside the memory store, and a failure to
close is counted rather than raised, because one stuck handle must not prevent
releasing the next.

## Running without a person

The interface used to be the only entry point, which quietly made three things
impossible: continuous integration, scripting, and subagents, since a subagent is
a headless agent. Composition is therefore separate from presentation.
`build_session` assembles the agent, tools, stores, and session manager and
returns them; `run_interactive` puts a Textual app in front of that, and the
one-shot path drives the same object and renders events to a stream.

The interesting problem is not plumbing, it is what approval means when nobody is
there to give it. Refusing everything makes the mode useless and approving
everything discards the execution platform, so an autonomy level names a risk
ceiling and the existing classification decides the rest. `read-only` permits
neither a file change nor a command, `edit` permits changes and commands up to
medium risk, and `full` permits up to high; critical is denied by policy before
autonomy is consulted. The default is the most restrictive one, because an
operator who did not choose should get the safe answer.

A refusal is stated rather than silent. Each one names the tool and the ceiling
it exceeded, printed to standard error, so a run that did less than expected
explains why instead of looking like the model gave up.

This is also what made the risk taxonomy matter. When every default request
carried a high-risk finding for touching the host filesystem, `ls` and
`rm -rf build` scored identically, and no ceiling built on that could
discriminate. Request shape now states what access was granted without inflating
risk, and the command decides the level.

## Scoring the agent

Every improvement in this project so far was found by watching a session fail.
That finds real defects and cannot answer whether a change made the agent better.

An evaluation task is a set of files, a prompt, and a check that inspects the
workspace afterwards. The runner materialises each task into a throwaway
directory, runs one turn, and applies the check to what is on disk. Checks assert
outcomes, not calls: that a file now contains something, or that a file the task
forbade touching is byte-identical. A task that asks a question ships with a check
that nothing changed, because a read-only request that edits anyway has failed
even when the answer is right.

One task never stops the suite. A factory that raises, a turn that errors, and a
check that fails all become the same thing: one failed result carrying a reason.

## Delegation

A subagent shares the workspace and nothing else. It gets a fresh state, a fresh
context, its own iteration budget, and a tool registry without `delegate` in it,
so depth is bounded structurally rather than by a counter anyone could forget.

Only the final reply crosses back. The subagent's transcript, its tool results,
and the files it read stay in its own context, which is the entire point: the
parent pays for one report instead of the whole investigation.

The tool defines the contract and the agent layer implements it. `delegate` knows
about a runner that takes a task and returns a reply, a call count, and an
optional error; it does not import the agent, because tools do not depend on
outer layers. The composition root supplies a runner that drives a real agent and
maps its event stream into that shape.

Approval is inherited, not bypassed. The subagent uses the parent's approval
handler, so a delegated command reaches the same person or the same autonomy
ceiling as a direct one. Delegation moves work off the parent's context; it does
not move it outside the gate. A subagent that fails returns an error the parent
can read and react to, rather than a silence it has to interpret.

## Choosing a model

Which model answers used to be decided by `os.getenv` in two places: the client
read `API_KEY` and `BASE_URL` once and cached the connection forever, and re-read
`MODEL` on every request. That combination cannot be changed at runtime. Setting a
new model would take effect while the cached connection still pointed at the old
provider, which is a bug waiting for the first person to switch.

Settings are therefore resolved once and handed to the client rather than fetched
by it. A credential knows how to turn itself into client options and how to
describe itself without revealing itself, so a key can be shown in the interface
as its last four characters and never in full. A provider knows its base URL and
where its model list lives. The active selection holds both plus the model.

The seam is drawn where invalidation matters. Changing the model is free, because
the same connection serves any model the provider offers. Changing the provider or
the credential fires a listener that drops the cached client, so the next request
builds a new one. That distinction is the whole reason the object exists, and it
is what a naive "just store the model somewhere" would have missed.

The environment is one credential source rather than the only one. Existing `.env`
setups resolve through the same path, and a credential is a protocol rather than a
class, so an API key and an OAuth token are interchangeable to everything above
them. Resolution has a fixed order: a remembered selection outranks `MODEL` from
the environment, because a choice made in the interface is more recent than a file
written once, and a stored token outranks an API key only when it is still usable.

A selection that vanishes at exit is not a selection. `/models` writes the chosen
model to its own small file, and startup reads it before the environment. That file
is parsed as strictly as every other configuration, and a corrupt one is ignored
rather than repaired, because falling back to the environment is always safe while
guessing at a damaged file is not.

A provider's model list is third-party JSON and is treated as such: the count is
capped, identifiers and names are length-bounded, context windows outside a
plausible range are dropped, and duplicates are collapsed. The result is cached to
disk with a timestamp and a six-hour lifetime, because this is a network call and
it must not happen at launch or on a keystroke. A cache that is corrupt, truncated,
or stale is ignored rather than repaired.

## Commands typed into the composer

Anything beginning with a slash is intercepted before it reaches the agent. That
ordering is the point: a mistyped command must not become a prompt, and an unknown
one must say so rather than being quietly answered by the model.

Commands are a registry rather than a chain of conditionals, so `/help` can
enumerate them and every future command inherits parsing, casing, and the unknown
command message for free. A command is one word plus an optional argument, and a
multi-line input is never a command, because a prompt that happens to start with a
slash is far more likely than a command someone wrapped across lines.

A registry is only useful if someone can find what is in it. Requiring the whole
name to be typed correctly means the only way to learn a command is to already
know it, and `/help` is itself a command you have to know. The composer therefore
offers the matching commands as soon as a slash is typed and narrows them with
every further character: `/` lists all of them, `/q` leaves `quit`, and `/mo`
leaves `models` and `model`. The offer closes once a space is typed, because from
that point the text is an argument rather than a name, and it closes on a prefix
nothing matches, which is the same signal the unknown command message gives after
submission but arrives before it.

Tab completes to the longest prefix the remaining matches share, so `/q` becomes
`/quit` outright while `/l` becomes `/log` and waits for the character that
decides between `login` and `logout`. Completing to a shared prefix rather than
cycling through candidates means the key never guesses: what it inserts is text
the user was going to type regardless. When no command is being typed, tab keeps
its ordinary meaning and moves focus.

Filtering and completion are pure functions over the typed text, so both are
decided without a widget, a screen, or a running application, and the interface
layer only applies the result. `/quit` routes to the same application action as
the keyboard shortcut rather than tearing down the interface itself, which is why
a command and a keypress cannot drift into two different shutdown paths.

## Signing in with a browser

An API key is a string the user already has. A browser sign-in is a protocol, and
the grant matters: a terminal program cannot keep a client secret, so this is
authorization code with PKCE rather than the implicit or client-credentials flows
that would require one.

The parts that carry the security are small and separately testable. A fresh
verifier is generated per attempt and its SHA-256 challenge is what travels to the
provider, so the value that proves the exchange never leaves the process. A random
`state` is issued and compared with `secrets.compare_digest` on return, and a
mismatch is refused before the code is read, because a callback with someone else's
state is the shape of a cross-site request forgery. The callback server binds
loopback only, answers exactly one request, and closes.

Tokens are stored per provider using the same permission discipline as the audit
stores: mode `0600` on POSIX, and on Windows an explicit ACL granting only the
current user, since mode bits express nothing there. The file is written to a
temporary path inside the already-secured directory and moved into place, so it is
never briefly readable while being written.

They are also token-shaped, which means the environment allowlist already strips
them from every child process, so a stored credential cannot ride along into a
shell command. A token knows its own expiry and whether a refresh is possible, so
an expired credential with no refresh is treated as absent rather than presented
and rejected by the provider.

TrueCoder registers no OAuth client of its own. The `client_id` and both endpoints
come from configuration, which keeps the mechanism general and leaves the question
of which providers permit a third-party client where it belongs, with the person
reading that provider's terms.

## Design rules

Keep these invariants stable as the codebase grows:

* completed history contains only valid turns
* context is recent, contiguous, and turn-based
* the task plan is reprojected into every request and never trimmed as history
* a tool result is bounded against the conversation, not only against itself
* shortening changes what is sent and never what is stored
* a user's own words are never truncated to make room
* a checkpoint covers the workspace, never only the mutations that were recorded
* a snapshot never touches the user's index, branch, or working tree
* restoring captures the current state first, so a restore can be undone
* a restore names what it will remove before it removes it
* a checkpoint round trip is byte exact, never normalised on the way through
* memory is scoped by workspace and never crosses between repositories
* what memory tells the model is exactly what the user can read and delete
* a note is corrected in one step, never left contradicting its replacement
* uniqueness is decided by a normalised key, and the note is stored as written
* a durable change to future behaviour is approved, like a durable file change
* unreadable memory degrades a reply rather than failing a turn
* there is one path for running a command, and hooks use it
* a hook is authorised by configuration, never by a per-call prompt
* pre-authorisation names one execution and is released even on failure
* a hook that fails is reported and never blocks the turn
* a turn diff compares tree against tree, never a tree against the index
* a turn diff is anchored to the state before this turn, or to nothing
* a file the user left untracked is never reported as something a turn did
* file size is checked before content is read
* checkpoints are unavailable rather than approximated when git cannot back them
* repetition is detected by what was called and what came back, never by call id
* a stalled turn withdraws tools instead of discarding the turn
* tool calls that were never offered are never executed
* legitimate repetition is given more room than identical repetition
* evicted turns are summarised rather than silently forgotten
* a failed summary loses no history
* a summary is labelled as history and never as instruction
* code intelligence resolves names by compiler, never by text match
* positions are one-based at the tool boundary and zero-based on the wire
* only capabilities the client actually implements are ever claimed
* a language server can read the workspace and never write to it
* the plan costs tokens before history is selected, never after
* at most one plan step is in progress at a time
* an invalid plan update leaves the previous plan intact
* the plan is scratch: it never persists across a session switch or restart
* transcript order follows the agent event stream, not a queued message
* text the user or the model produced is rendered as text, never as markup
* a tool call the model gets wrong is reported to it, never raised at the user
* what will not fit is dropped whole, never cut wherever the edge falls
* a widget that may already be unmounted is updated without raising
* every durable handle the agent was given is released when it closes
* tool calls always have matching results
* only publicly routable addresses are reachable, by allowlist not blocklist
* every resolved address is validated, not only the one that gets used
* a connection is pinned to an address that was validated
* every redirect hop clears the same checks as the original URL
* fetched bytes and extracted text are bounded independently
* retrieved content is returned as data and never as instructions
* a file change is reviewed as a diff, never as raw serialized arguments
* a mutation preview is read-only and never blocks or alters approval
* the approval fingerprint covers the effect, never the rendered preview
* rendered diffs stay bounded as the underlying change grows
* an empty diff means identical text, never merely identical line content
* every applied write and edit is recorded with both file digests
* mutation records are insert-only evidence
* a mutation that is already durable is never reported as failed
* provider-specific behavior stays inside the client
* tools do not depend on outer layers
* the UI does not contain agent logic
* approvals apply to exact fingerprints, never tool names alone
* unsafe shell requests cannot receive reusable approval scopes
* cancellation is addressed by execution ID and is idempotent
* policy can tighten model requests but cannot weaken configured ceilings
* child environments are constructed from an allowlist, never copied wholesale
* output byte counts and digests cover the full raw streams
* retained and model-visible output remain bounded as produced output grows
* backend ownership transfers only through a successfully returned handle
* project-controlled code stays gated until its resource identity is durable
* POSIX project commands run in a supervisor-owned process group
* closing the POSIX parent-lifetime pipe terminates and reaps the project tree
* local POSIX and Windows execution never claim filesystem or network isolation
* discovery facts are bounded, explicit, and separated from pure selection
* backend selection compares every capability requirement independently
* a default that no local backend can satisfy silently forces the sandbox
* the machine the agent runs on is stated, never left to be discovered
* a request nothing can run names what refused it and why
* a permitted critical command is refused on the host, never run unprotected
* policy raises a requirement and never rewrites the request to meet it
* a tool call that never started still shows what it tried to run
* a third-party schema is bounded before the model ever sees it
* a server tool is namespaced, so it can never shadow a built-in
* strictness is claimed only for a schema this project authored
* a server's output is data, and is labelled as data every time
* one failing tool server never stops another, or the application
* a path rooted under either platform convention is refused under both
* a budget too small to hold one ordinary file makes the agent re-read forever
* the system prompt teaches the agent to work, it does not describe the agent
* composition is separate from presentation, so the agent can run without a UI
* with nobody watching, the safe answer is the default answer
* an unattended refusal is stated, never silent
* risk describes the command, never the shape every command shares
* a task is scored by what it left on disk, not by which calls it made
* one failed task never stops the suite
* a subagent shares the workspace and nothing else
* only a subagent's reply crosses back, never its transcript
* a subagent cannot delegate, and inherits the approval it cannot bypass
* several edits to one file land together or not at all
* the client is given its settings and never reaches for them
* changing a model is free, changing a connection invalidates it
* a provider's model list is bounded before it is shown or cached
* a slash command is answered here, never sent to the model
* a credential is a protocol, so how you authenticate never reaches the client
* a remembered choice outranks the environment, a broken file outranks nothing
* the PKCE verifier never leaves the process
* a callback with the wrong state is refused before its code is read
* a stored token is private on disk and stripped from every child process
* tests never write to the real configuration directory
* an explicit backend or shell preference is never silently downgraded
* execution cannot start before its pending audit evidence is durable
* every admitted execution ends in one immutable terminal audit state
* no backend re-resolves environment, shell, limits, or capabilities
* a backend runs only when its descriptor still matches the prepared one
* terminal outcomes are ranked by fixed priority, never by task scheduling
* the first terminal claim wins and can never be replaced
* exactly one owner reads the backend output iterator
* command timeouts are outcomes; internal safety deadlines are failures
* audit finalization failure withholds the result instead of faking one
* incomplete cleanup is an error, never a successful command result
* every task the runner creates is awaited or cancelled before it returns
* an execution is cancellable by ID from admission, not only once it starts
* pre-start cancellation returns cancelled and records that nothing started
* lifecycle events are bounded and never block or fail an execution
* only implemented and tested container dialects ever become available
* the sandbox image is digest-pinned and launch never pulls
* project code stays stopped until its container identity is durable
* container recovery acts only on a full immutable ID plus an exact label match
* startup recovery acts only on exact persisted backend resource identities
* the shell adapter never reimplements execution policy or process management
* every shell invocation uses the active call, session, turn, and workspace IDs
* outer cancellation waits for execution cleanup before aborting the turn
* shell approval happens once, after effective capabilities and limits exist
* normal nonzero exits remain bounded successful tool payloads
* shell is advertised only after audit, recovery, and a backend are healthy
* shell-specific prompt guidance exists only while the tool is registered
* plan-specific prompt guidance exists only while a plan store is attached
* session saves happen only at completed-turn boundaries
* restoring a session cannot partially replace agent state
* presentation sinks never block, delay, or fail an execution
* every lifecycle stage has an explicit presentation and unknown stages are refused
* retained interface output stays bounded as produced output grows
* the interface can disappear at any lifecycle point without leaking a process
* an awaited approval is always resolved, including during shutdown
* approval controls are armed only once the decision has somewhere to go
* cancellation from the interface addresses one execution id, not the turn
* a platform reports what it cannot enforce instead of implying a weaker version
* macOS never applies a per-user process rlimit as a process-tree limit
* project code stays suspended until its Job Object identity is durable
* a Job Object is always killed on close so descendants cannot be orphaned
* native Windows support is exercised through contract and integration suites on Windows
* persisted Win32 handle values are never trusted across a process restart
* trusted-command rules can only tighten policy, never widen it
* invalid trusted-command rules make execution unavailable rather than being ignored
* optional execution configuration has safe defaults and a strict versioned schema
* a network-enabled container is never selected without a configured isolated network
* audit retention never deletes nonterminal evidence
* retention preserves schema invariants by atomically replacing a verified database
* audit details matching credential rules render only as redacted
* model-visible failure reasons are bounded and never expose arbitrary native diagnostics
* execution cards show the bounded requested command from the first lifecycle event
* execution health and audit history remain visible even when shell is unavailable
