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
 └─ Tools

Shell tool (future phase)
 ↓
Execution service
 ├─ Execution context
 ├─ Cancellation registry
 ├─ Durable audit service
 ├─ Pure policy, environment, and output components
 ├─ Backend discovery and capability selection
 └─ Platform backends (future phases)

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

Approval-required calls are prepared once. Preparation resolves the tool and
parses its arguments. The approval fingerprint, approval display, and eventual
execution all use that same prepared value, so the system never approves one
parse and executes another.

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

This layer does not claim to execute or sandbox commands. Process creation and
platform backends remain later execution phases.

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

Phase 5 defines one backend lifecycle without implementing a model-facing shell
or concrete process backend. `ExecutionBackend.start()` accepts the immutable
request, execution context, and read-only cancellation token. A successful
start returns an `ExecutionHandle` with the matching execution ID, one exact
durable `BackendResourceIdentifier`, a single-owner raw output iterator, stable
wait semantics, idempotent termination, and idempotent cleanup.

Cleanup ownership transfers exactly once:

1. During `start()`, the backend owns every partially acquired native resource.
2. A failed start cleans those partial resources before raising.
3. A successful return transfers all native ownership to the handle.
4. The execution service owns that handle's lifecycle and must eventually call
   cleanup after normal exit, termination, cancellation, or monitoring failure.

Termination, waiting, and cleanup are separate operations. Termination asks the
complete native resource to stop. Waiting observes and reaps its terminal state.
Cleanup releases pipes, handles, temporary state, and other remaining
resources. Repeating any of these operations must not target a guessed or reused
native identifier. A reusable backend contract suite encodes these invariants
before the POSIX, Windows, or container adapters exist.

Discovery happens through an injected `DiscoveryIO` boundary. Unit tests can
model Linux, macOS, Windows, and unknown hosts without depending on the CI
runner. `SystemDiscoveryIO` is the only Phase 5 object that inspects the actual
machine. It detects:

* normalized operating-system family, architecture, and release
* canonical installed POSIX, PowerShell, and Windows command-shell paths
* Linux cgroup v2 mount, controller, membership, and delegated writability facts
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

Capability matching is pure. It compares every field in Phase 4's
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
preserves the reasons for every permitted candidate.

`BackendDescriptor.available` means the host prerequisites for that adapter
were discovered. It does not mean Phase 5 can start a command. Concrete POSIX,
Windows Job Object, and container implementations arrive in later phases and
must pass the shared contract suite before the execution service can register
them.

## Durable execution audit

The audit subsystem is the evidence boundary for future shell execution. An
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
handlers arrive with their respective backend phases.

## Design rules

Keep these invariants stable as the codebase grows:

* completed history contains only valid turns
* context is recent, contiguous, and turn-based
* tool calls always have matching results
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
* discovery facts are bounded, explicit, and separated from pure selection
* backend selection compares every capability requirement independently
* an explicit backend or shell preference is never silently downgraded
* execution cannot start before its pending audit evidence is durable
* every admitted execution ends in one immutable terminal audit state
* startup recovery acts only on exact persisted backend resource identities
* session saves happen only at completed-turn boundaries
* restoring a session cannot partially replace agent state
