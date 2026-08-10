# TrueCoder

> A terminal coding agent with an auditable execution plane: every command it runs is policy-checked, approved, bounded, sandboxable, and durably recorded before a single byte of project code executes.

[![Tests](https://github.com/Shivam583-hue/TrueCoder/actions/workflows/tests.yml/badge.svg)](https://github.com/Shivam583-hue/TrueCoder/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)](#prerequisites)
[![Ruff](https://img.shields.io/badge/ruff-check%20passing-2ea44f?style=flat-square&logo=ruff&logoColor=white)](#available-commands-and-scripts)
[![Sandbox](https://img.shields.io/badge/sandbox-Linux%20%C2%B7%20Docker%20certified-2496ed?style=flat-square&logo=docker&logoColor=white)](#container-sandbox-image)
[![CI](https://img.shields.io/badge/CI-linux%20%C2%B7%20macos%20%C2%B7%20windows-blue?style=flat-square&logo=githubactions&logoColor=white)](#testing)

TrueCoder is a Python agent runtime that reads, searches, edits, and runs code inside one project.
It ships a Textual terminal interface, an OpenAI-compatible LLM client, persistent SQLite sessions, fifteen approval-gated tools plus any MCP servers you configure, a task planner, language-server code intelligence, workspace checkpoints, durable memory, user-configured hooks, and an execution subsystem that treats running a command as a security event rather than a subprocess call.
Shell execution passes through policy evaluation, capability-based backend selection, an approval fingerprint, a durable audit admission, a resource launch gate, arbitrated terminal outcomes, and one immutable terminal audit record.
Commands run on your machine by default, with the toolchain, virtual environments, and caches you already have, because the approval gate is the security boundary and an agent that cannot run your test suite is not useful.
When a command must be isolated instead, the certified sandbox profile runs it in a digest-pinned, non-root, read-only, network-denied, capability-dropped Docker container that is proven against real Docker rather than assumed safe.

## Table of contents

- [Demo](#demo)
- [Key features](#key-features)
- [Repository structure](#repository-structure)
- [Engineering scorecard](#engineering-scorecard)
- [Technical highlights](#technical-highlights)
- [Architecture overview and diagram](#architecture-overview-and-diagram)
- [Technology stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Local development setup](#local-development-setup)
- [Environment variables](#environment-variables)
- [Container sandbox image](#container-sandbox-image)
- [Available commands and scripts](#available-commands-and-scripts)
- [Testing](#testing)
- [Runtime data and storage](#runtime-data-and-storage)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Demo

Coming soon...

## Key features

- **Terminal-native agent** - a Textual TUI with streaming responses, live tool cards, inline approvals, cancellation, and token accounting.
- **Turn-based conversation model** - only complete, valid turns enter history, so a tool call never survives without its result.
- **Persistent project-scoped sessions** - completed turns are stored in SQLite outside the repository and restored transactionally, and one repository can never list or resume another repository's sessions.
- **Fifteen approval-gated tools** - `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `shell`, `web_fetch`, `find_symbol`, `goto_definition`, `find_references`, `get_diagnostics`, `remember`, `forget`, and `delegate`, each with its own validated schema and security boundary. `edit_file` takes a list of edits applied together, so a multi-site change costs one call and one approval and either lands whole or not at all. A tool call the model gets wrong comes back as an error it can read and retry, so a bad argument costs one call rather than the turn.
- **A context budget that is actually enforced** - a single shell or fetch result can exceed the whole token budget, so oversized tool results are shortened where the request is assembled, into a valid envelope that says how much was dropped. The stored turn, the session record, and the audit keep the complete result.
- **Memory you can read, correct, and delete** - `remember` records a durable fact about the project and `forget` drops one, both approval-gated because they change behaviour in future sessions. A note that stops being true is corrected in one step with `replaces`, so a correction never leaves the old version contradicting the new one on every later turn. Notes are keyed case- and punctuation-insensitively so trivial variants cannot crowd out real facts, they are scoped to one workspace, projected into every request, and `ctrl+n` shows exactly what the model is being told.
- **Hooks that run inside the execution plane** - a versioned `hooks.json` can run your formatter or linter at turn start or after a turn that changed files. Because you wrote the config, a hook is pre-authorised rather than prompting, but it is still bounded, policy-checked, and written to the same durable audit as any other command.
- **See what a turn actually changed** - `ctrl+d` diffs the workspace against the pre-turn checkpoint, so a turn's real effect on disk is visible even when files were changed by a shell command rather than by the reviewed edit tools. The mutation audit records what `write_file` and `edit_file` did; this records what happened.
- **Undoable turns** - a checkpoint of the whole workspace is captured before every turn using git plumbing, so a turn can be reversed even when the agent changed files through `shell` rather than through the reviewed edit tools. Restoring first captures the current state, so a restore is itself undoable.
- **Loop detection, not just a cap** - identical tool calls returning identical results are recognised as a stall, the tools are withdrawn so the model must answer with what it has, and a model that ignores the withdrawal is stopped rather than allowed to keep spending. A stuck agent that previously burned 25 model requests and then failed the turn now costs 4 and still answers.
- **Rolling compaction instead of silent forgetting** - when history outgrows the budget, the oldest turns are summarised into a running summary rather than dropped, and the summary is labelled as history so it is never mistaken for instructions.
- **Language-server code intelligence** - `find_symbol`, `goto_definition`, `find_references`, and `get_diagnostics` resolve names the way a compiler does instead of by text match, over a real LSP session with server discovery, stdio JSON-RPC framing, document synchronisation, and lifecycle management. This first version is strictly read-only.
- **A task planner that survives context eviction** - `update_plan` keeps an ordered checklist with exactly one step in progress, and the current plan is reprojected into every model request instead of being left in history to roll off the token budget.
- **SSRF-resistant web access** - `web_fetch` allows only publicly routable addresses rather than blocking a list of bad ones, validates every DNS record before connecting, pins the connection to a validated address, and re-validates each redirect hop, so loopback, private ranges, and cloud metadata are refused by construction.
- **Fetched text is data, never instructions** - a page arrives with an explicit untrusted-content notice and prompt guidance that forbids following instructions found inside it.
- **Reviewable file mutations** - `write_file` and `edit_file` render a real unified diff in the approval card, with hunk headers, line numbers, and colored gutters, so a code change is reviewed as a diff rather than as escaped JSON arguments.
- **Durable mutation evidence** - every applied write and edit is recorded in its own immutable SQLite store with SHA-256 digests of the file before and after, byte counts, line deltas, and the originating call, turn, session, and workspace.
- **Atomic filesystem edits** - `write_file` and `edit_file` write beside the destination and install with `os.replace()`, so a reader sees the old complete file or the new complete file and never a partial one.
- **Concurrency-aware editing** - `edit_file` verifies device, inode, size, and modification time before replacement and reports `file_changed` instead of overwriting a file that moved underneath it.
- **Fingerprinted approvals** - approval covers canonical arguments, workspace identity, limits, backend, capabilities, risk, and policy version, so changing any of them requires approving again.
- **Policy-evaluated execution** - ordered rules classify read-only, test, build, package, network, deletion, permission, Git, script, and unknown commands, and requested limits can only tighten the configured ceiling.
- **Capability-matched backends** - discovery measures the real host, and selection compares every capability requirement independently instead of trusting optimistic class constants.
- **Switch models without restarting** - type `/models` to pick from everything your provider lists, filtered as you type and annotated with context windows. The list comes from the provider's own `/v1/models`, bounded like any other untrusted response, and cached for six hours so it never costs a request at launch. The choice is written to `settings.json` and survives a restart. `/models refresh` refetches, `/model` says what is answering now, `/help` lists what you can type.
- **Two ways to authenticate** - an API key from the environment, or `/login` to authorise in your browser. The browser flow is OAuth 2.0 authorization code with PKCE, which is the correct grant for a program that cannot keep a secret: the verifier never leaves the process, the callback listens only on loopback for a single request, and a mismatched `state` is refused. Tokens are written privately in your config directory, `0600` on POSIX and ACL-restricted to your user on Windows, and are stripped from every child process environment by the same rule that strips any other credential.
- **Runs without a terminal** - `truecoder -p "fix the failing tests"` runs one prompt, prints the reply, and exits nonzero if the turn failed, so the agent works in CI and in scripts. With nobody watching, what may proceed is a configured decision rather than an accident: `--autonomy read-only|edit|full` sets a risk ceiling, anything above it is refused with a stated reason, and read-only is the default.
- **Scored, not vibed** - `truecoder --eval` runs a fixed set of tasks in throwaway workspaces and reports how many passed, so "did that change help?" has an answer. Each task asserts an outcome on disk rather than which calls were made.
- **Delegation with a hard boundary** - `delegate` hands a self-contained subtask to a fresh agent that shares the workspace but starts with an empty conversation. Only its final reply crosses back, never its transcript, it cannot delegate again, and it is approval-gated like any other tool.
- **MCP servers, treated as untrusted** - configured servers contribute their tools through the same registry, approval fingerprint, and audit as everything else. Their schemas are bounded before the model ever sees them, their names are namespaced so nothing can shadow a built-in, and their output is labelled as third-party data the model must never take instructions from. A server that fails to start is reported and skipped; it never stops the others or the application.
- **A system prompt that teaches the agent to work** - learn how the repository builds and tests itself before running anything, never install a tool to make a command succeed, treat a shortened result as an instruction to read a narrower range rather than the same one again, and remember that every call spends a human approval. Each rule is there because its absence was observed costing a turn.
- **The agent knows what machine it is on** - the working directory, operating system, interpreter, and any workspace virtual environment are gathered at startup and stated in the system prompt, so the model runs your test suite through the right interpreter instead of probing for it or guessing.
- **Nothing is downloaded between typing `truecoder` and seeing it** - the token encoding that context budgeting needs is a 3.6 MB fetch, so it is loaded on first use rather than at construction, warmed on a background thread while the interface paints, and cached in your cache directory instead of the temporary directory that a reboot clears. If it cannot be fetched at all, counting falls back to an estimate that over-counts rather than under-counts, so an offline launch degrades instead of failing.
- **Useful by default, isolated on request** - shell commands run locally so the project's dependencies are actually present; asking for the container, for a non-host filesystem mode, or for no network opts into the sandbox instead. A request no backend can satisfy names the backend that refused it and why, rather than failing as a generic infrastructure error.
- **Critical commands cannot run unprotected** - a command that reaches critical risk and is still permitted has its isolation raised beyond anything a local backend provides, so it is refused on the host and must be moved into the sandbox deliberately. Set `unknown_risk` to `critical` and every unrecognised command is held to that bar.
- **Proven container sandbox** - non-root UID 65532, read-only root filesystem, approved tmpfs only, denied network, all capabilities dropped, no-new-privileges, and a digest-pinned image that launch never pulls.
- **Durable execution audit** - a WAL-journaled SQLite evidence store with an immutable event log, trigger-protected rows, exactly one terminal outcome per run, and SHA-256 digests over the full raw output streams.
- **Operational evidence controls** - startup recovers nonterminal runs first, then atomically compacts expired terminal evidence while preserving schema triggers and every unresolved record.
- **Launch gating** - project-controlled code stays blocked behind a private gate until its exact backend resource identity is committed to the audit store.
- **Crash recovery** - startup leases every nonterminal audit row and acts only on exact persisted identities, never on a guessed or reused PID.
- **Bounded everything** - output, previews, environment allowlists, scan limits, match counts, and lifecycle event buffers all stay bounded as the underlying quantity grows.
- **Secret hygiene** - child environments are built from an allowlist rather than copied, credential-shaped names are removed, and values are never copied into audit metadata or tool results.
- **Live execution cards** - one evolving card per command, driven entirely by typed lifecycle stages, with bounded streaming output, responsive cancel by execution id, and the audit id on completion.
- **Actionable execution status** - refused and failed-start results carry bounded reason codes and messages, while `ctrl+e` explains audit, recovery, and backend health.
- **Compact approvals** - seven decision facts by default (command, directory, backend, access, limits, risk, scope), with the full capability contract behind the expander.
- **Safe shutdown** - closing the interface resolves any awaited approval, cancels active executions by id, and waits a bounded window for cleanup and audit finalization.
- **Cross-platform local execution** - POSIX process groups manage Linux and macOS commands, Windows Job Objects manage Windows process trees, and every native backend is exercised in CI on its own operating system.
- **Honest platform reporting** - unsupported capabilities remain explicit; for example, macOS reports `process_limits` as unsupported rather than applying a per-user rlimit as if it bounded a process tree.
- **Trusted-command rules** - versioned, user-editable rules that can only tighten policy, stored privately and parsed strictly.
- **Audit viewer** - filter runs by outcome, backend, recency, and text, with redacted secret-shaped details and cleanup status surfaced ahead of the exit code.
- **Project instructions** - `AGENTS.md` and `AGENTS.override.md` are discovered from the Git project root down to the launch directory and injected into the system prompt.

## Repository structure

TrueCoder is a single `src`-layout Python package.
Application code lives under `src/truecoder/`, the sandbox image is built from `container/`, and the test suite is split by the kind of guarantee each layer proves.

```text
TrueCoder/
├── .github/
│   └── workflows/
│       └── tests.yml                  # Linux, sandbox, macOS, and Windows CI matrix
├── .env.example                       # LLM provider template
├── .gitignore
├── AGENTS.md                          # Project instructions injected into the system prompt
├── README.md
├── pyproject.toml                     # Package metadata, dependencies, and the console script
│
├── container/                         # Execution sandbox image
│   ├── Dockerfile                     # Digest-pinned base, non-root UID 65532, trusted entrypoint
│   ├── entrypoint.py                  # In-container supervisor and CPU accounting
│   ├── image.lock                     # Pinned content digest, platform, user, entrypoint version
│   └── README.build.md                # Build, lock, verify, and rebuild policy
│
├── docs/
│   └── ARCHITECTURE.md                # Authoritative design reference and invariant list
│
├── src/truecoder/
│   ├── __main__.py                    # python -m truecoder
│   │
│   ├── agent/                         # Orchestration
│   │   ├── agent.py                   # Agent loop, tool invocation contexts, and app launch
│   │   ├── context.py                 # Turn-based context selection and token budgeting
│   │   ├── tokenizer.py               # Lazy, durably cached tokenizer with an offline fallback
│   │   ├── budget.py                  # Shortening oversized tool results to fit
│   │   ├── progress.py                # Repeated-call and no-progress detection
│   │   ├── compaction.py              # Rolling summary of evicted turns
│   │   ├── state.py                   # Active turn and completed history
│   │   ├── messages.py                # Durable model message types
│   │   ├── events.py                  # Agent event stream consumed by the UI
│   │   ├── approval.py                # Agent-side approval routing
│   │   ├── prompts.py                 # System prompt and conditional tool guidance
│   │   ├── environment.py             # Startup facts about the machine and its interpreters
│   │   ├── autonomy.py                # What may proceed with nobody watching
│   │   └── project_instructions.py    # Project root discovery and AGENTS.md loading
│   │
│   ├── memory/                        # Durable per-workspace notes
│   │   ├── models.py                  # Note bounds and rendering
│   │   └── store.py                   # Workspace-scoped SQLite, keyed notes, schema migration
│   │
│   ├── hooks/                         # User-configured commands
│   │   ├── models.py                  # Hook shape, events, and outcomes
│   │   ├── configuration.py           # Strict, fail-closed hooks.json
│   │   └── runner.py                  # One hook through the execution service
│   │
│   ├── planning/                      # Dependency-free task plan domain
│   │   ├── models.py                  # Plan and step invariants, bounds, and rendering
│   │   └── store.py                   # The single in-memory plan for the active task
│   │
│   ├── checkpoint/                    # Workspace snapshots and restore
│   │   ├── git.py                     # Snapshots through a temporary index
│   │   ├── models.py                  # Checkpoint identity and restore outcome
│   │   ├── changes.py                 # Working tree against a checkpoint tree
│   │   └── service.py                 # Capture, list, prune, restore, compare
│   │
│   ├── cli.py                         # Interactive launch, one-shot prompts, and scoring
│   │
│   ├── providers/                     # Where a model and its credentials come from
│   │   ├── models.py                  # Credentials, providers, and the active selection
│   │   ├── catalog.py                 # Bounded model discovery with a cached TTL
│   │   ├── configuration.py           # Strict providers.json, fail-closed
│   │   ├── oauth.py                   # PKCE, the callback contract, and token lifetime
│   │   ├── login.py                   # Loopback callback server and the browser round trip
│   │   ├── store.py                   # The remembered model selection
│   │   └── tokens.py                  # Private token storage, one entry per provider
│   ├── workspace.py                   # One containment rule for workspace-relative paths
│   │
│   ├── evaluation/                    # Fixed tasks scored in throwaway workspaces
│   │   ├── models.py                  # Tasks, outcome checks, and the report
│   │   ├── runner.py                  # One task per temporary workspace
│   │   └── tasks.py                   # The shipped task set
│   │
│   ├── jsonrpc/                       # Shared JSON-RPC over stdio
│   │   ├── framing.py                 # Framing contract and neutral message builders
│   │   └── transport.py               # Process lifecycle and request routing
│   │
│   ├── mcp/                           # Model Context Protocol tool servers
│   │   ├── protocol.py                # Newline framing and MCP method payloads
│   │   ├── schema.py                  # Bounds an untrusted server's tool schema
│   │   ├── models.py                  # Defensive parsing of listings and results
│   │   ├── client.py                  # Handshake, tool listing, tool calls
│   │   ├── tool.py                    # Namespaced registry adapter
│   │   ├── configuration.py           # Strict mcp.json, fail-closed
│   │   └── manager.py                 # One client per server, started at launch
│   │
│   ├── lsp/                           # Language server integration
│   │   ├── protocol.py                # Content-Length framing for language servers
│   │   ├── client.py                  # Handshake, document sync, queries
│   │   ├── discovery.py               # Servers on PATH, matched by language
│   │   ├── manager.py                 # One server per language, started lazily
│   │   └── models.py                  # Positions, symbols, diagnostics, URIs
│   │
│   ├── web/                           # Outbound network boundary
│   │   ├── policy.py                  # Scheme, host, and public-address rules
│   │   ├── fetch.py                   # Pinned, redirect-checked, bounded client
│   │   └── extract.py                 # Markup to readable text, pre-blocks kept
│   │
│   ├── mutation/                      # Dependency-free file-change domain
│   │   ├── models.py                  # Hunks, lines, and bounded diff limits
│   │   └── diff.py                    # Pure unified diff with context and truncation
│   │
│   ├── client/
│   │   ├── llm_client.py              # OpenAI-compatible streaming and non-streaming calls
│   │   └── response.py                # Provider responses translated into internal events
│   │
│   ├── tools/
│   │   ├── base.py                    # Tool definitions, arguments, calls, and results
│   │   ├── context.py                 # ToolInvocationContext bound to call, turn, and session
│   │   ├── executor.py                # Validate, prepare once, approve, then execute
│   │   ├── registry.py                # Explicit registration and lookup
│   │   ├── serialization.py           # Deterministic tool payloads
│   │   ├── mutation_audit.py          # Immutable evidence for every applied change
│   │   └── builtin/
│   │       ├── filesystem.py          # Shared sensitive-path and containment policy
│   │       ├── delegate.py            # A bounded subtask handed to a fresh agent
│   │       ├── read_file.py           # Bounded UTF-8 line ranges, 500-line default window
│   │       ├── write_file.py          # Atomic replacement, 32 KiB cap, diff preview
│   │       ├── edit_file.py           # Exact-text edits with concurrent-change detection
│   │       ├── list_dir.py            # Immediate children, 500 results, 5,000 scanned
│   │       ├── glob.py                # Rooted * and recursive ** path patterns
│   │       ├── grep.py                # Regex search with 200 matches and 20,000 scanned
│   │       ├── plan.py                # Whole-list plan replacement, no approval needed
│   │       ├── memory.py              # remember and forget
│   │       ├── web_fetch.py           # One public page as bounded readable text
│   │       ├── code_intelligence.py   # Symbols, definitions, references, errors
│   │       └── shell.py               # Model boundary for execution, not an executor
│   │
│   ├── execution/                     # Execution control plane
│   │   ├── models.py                  # Requests, limits, capabilities, contexts, results
│   │   ├── errors.py                  # Typed infrastructure and compatibility failures
│   │   ├── serialization.py           # Versioned execution-domain JSON envelopes
│   │   ├── context.py                 # Execution identity and workspace hashing
│   │   ├── cancellation.py            # Source and read-only token split
│   │   ├── clock.py                   # Injectable wall and monotonic time boundary
│   │   ├── configuration.py           # Optional strict operator policy; defaults need no setup
│   │   ├── registry.py                # Opaque execution ID to active entry
│   │   ├── policy.py                  # Ordered classification, limits, risk, and reasons
│   │   ├── environment.py             # Allowlist child environments and secret removal
│   │   ├── output.py                  # Bounded byte streams, decoding, sanitizing, redaction
│   │   ├── discovery.py               # Host, shell, cgroup, runtime, and image facts
│   │   ├── selection.py               # Pure capability matching and backend choice
│   │   ├── preparation.py             # One PreparedExecution no backend may re-derive
│   │   ├── lifecycle.py               # Validated internal state machine
│   │   ├── runner.py                  # Admission to one arbitrated terminal outcome
│   │   ├── results.py                 # Terminal material to audit and public result
│   │   ├── service.py                 # Public execute, cancel, and lookup surface
│   │   ├── approval.py                # Execution approval gate and safe-scope rules
│   │   ├── events.py                  # Bounded transient lifecycle publishing
│   │   ├── bootstrap.py               # Composition root and health report
│   │   ├── defaults.py                # Default limits: 120 s, 1 MiB output, 64 KiB return
│   │   ├── trusted_rules.py           # Versioned trusted-command rules that only tighten
│   │   │
│   │   ├── audit/                     # Durable evidence boundary
│   │   │   ├── schema.py              # Versioned SQLite schema and immutability triggers
│   │   │   ├── store.py               # WAL, immediate transactions, atomic finalization
│   │   │   ├── service.py             # Admission, events, attachment, finalization
│   │   │   ├── permissions.py         # 0700 directories and 0600 files, or unavailable
│   │   │   ├── output.py              # Hashed full streams with bounded previews
│   │   │   ├── recovery.py            # Leasing and terminal closure of nonterminal rows
│   │   │   ├── retention.py           # Cutoff policy for atomic terminal-evidence compaction
│   │   │   ├── models.py
│   │   │   └── codec.py
│   │   │
│   │   └── backends/
│   │       ├── base.py                # Shared backend and handle protocol
│   │       ├── models.py              # Descriptors, snapshots, resource identifiers
│   │       ├── registry.py            # get_exact refuses descriptor drift
│   │       ├── posix.py               # Linux and macOS local process backend
│   │       ├── posix_supervisor.py    # Session leader, gated project group, lifetime pipe
│   │       ├── posix_protocol.py      # Versioned length-prefixed JSON frames
│   │       ├── posix_plan.py          # Pure exec and shell launch planning
│   │       ├── posix_cgroup.py        # Delegated cgroup v2 subtree handling
│   │       ├── posix_limits.py        # cgroup hard limits and rlimit fallbacks
│   │       ├── posix_platform.py      # Honest Linux and macOS capability differences
│   │       ├── posix_identity.py      # Host, boot ID, and start-tick identity
│   │       ├── posix_recovery.py      # Exact-match-only recovery
│   │       ├── container.py           # Create, register, then start
│   │       ├── container_models.py    # Typed mounts, labels, image, and launch plans
│   │       ├── container_plan.py      # Pure mounts, labels, limits, and argv
│   │       ├── container_dialects.py  # Docker dialect only, by design
│   │       ├── container_runtime.py   # Runtime invocation boundary
│   │       ├── container_identity.py  # Label and ownership verification
│   │       ├── container_recovery.py  # Full immutable ID plus exact label match
│   │       ├── windows.py             # Job Object backend, handle, and recovery
│   │       ├── windows_native.py      # ctypes Win32 boundary, suspended launch gate
│   │       └── windows_plan.py        # Pure argv, quoting, and error normalization
│   │
│   ├── session/
│   │   ├── manager.py                 # Create, switch, rename, delete, and append turns
│   │   ├── store.py                   # Project-scoped SQLite in the user data directory
│   │   ├── codec.py                   # Validated encode and decode of durable turns
│   │   └── models.py
│   │
│   └── tui/
│       ├── app.py                     # Textual application, mount-time execution bootstrap
│       ├── execution_view.py          # Pure stage mapping, approval rows, bounded preview
│       ├── audit_view.py              # Workspace audit browser and bounded detail view
│       ├── execution_health.py        # Audit, recovery, and backend status screen
│       ├── widgets.py                 # Transcript, tool cards, plan card, and approvals
│       ├── sessions.py                # Session browser
│       ├── checkpoints.py             # Checkpoint browser and restore prompt
│       ├── changes.py                 # What this turn changed on disk
│       ├── memory.py                  # Memory browser and deletion
│       ├── commands.py                # Slash-command parsing and the registry
│       ├── model_picker.py            # Filterable model chooser
│       └── styles.tcss                # Terminal stylesheet
│
└── tests/
    ├── unit/                          # Mostly pure logic and injected platform fixtures
    │   ├── agent/                     # Loop, state, messages, composition, instructions
    │   ├── client/                    # Streaming, retries, and error translation
    │   ├── context/                   # Turn selection, token budgeting, plan projection
    │   ├── planning/                  # Plan and step invariants
    │   ├── mutation/                  # Diff hunks, bounds, and truncation
    │   ├── web/                       # URL policy, SSRF refusals, extraction
    │   ├── evaluation/                # Task checks, the runner, and the report
    │   ├── providers/                 # Credentials, selection, and catalog bounds
    │   ├── jsonrpc/                   # Transport lifecycle and request routing
    │   ├── mcp/                       # Framing, schema bounds, client, adapter, manager
    │   ├── lsp/                       # Framing, client, discovery
    │   ├── checkpoint/                # Snapshot, restore, prune, agent capture
    │   ├── memory/                    # Notes, scoping, pruning, projection
    │   ├── hooks/                     # Config parsing, runner, pre-authorisation
    │   ├── session/                   # Durable turn codec
    │   ├── tools/                     # Base, executor, registry, and every builtin tool
    │   ├── tui/                       # Presentation mapping, cards, and audit summaries
    │   └── execution/                 # Policy, environment, output, discovery, selection
    │       └── audit/                 # Store, permissions, recovery, and evidence
    ├── contract/                      # 41 scenarios: one contract, four backend adapters
    │   └── execution/
    │       ├── backend_contract.py
    │       ├── test_fake_backend_contract.py
    │       ├── test_posix_backend_contract.py
    │       ├── test_container_backend_contract.py
    │       └── test_windows_backend_contract.py
    ├── integration/                   # Real processes, SQLite, native Windows, and the TUI
    │   ├── execution/
    │   │   └── backends/
    │   │       └── test_windows_backend.py # Job cleanup, process limits, service audit
    │   ├── session/
    │   └── tui/
    ├── e2e/                           # A scripted model driving real tools on a real workspace
    ├── sandbox/                       # Adversarial checks against real Docker
    ├── fakes/                         # Deterministic backend and service doubles
    └── helpers/                       # Real child programs, including language and tool servers
```

### Where to make common changes

| Change                                | Primary location                                  | Usually also check                                                     |
| ------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------- |
| Terminal UI, transcript, or approvals | `src/truecoder/tui`                               | `styles.tcss`, agent events, and the TUI integration tests             |
| Agent loop or turn lifecycle          | `src/truecoder/agent/agent.py` and `state.py`     | Context builder, session codec, and unit agent tests                   |
| Provider behavior                     | `src/truecoder/client`                            | `response.py` event translation and client unit tests                  |
| A new tool                            | `src/truecoder/tools/builtin`                     | `builtin/__init__.py`, registration in `agent.py`, and tool tests      |
| Plan shape or invariants              | `src/truecoder/planning`                          | `builtin/plan.py`, `PlanCard`, plan projection in `context.py`         |
| Code intelligence                     | `src/truecoder/lsp`                               | `builtin/code_intelligence.py` and the fake server in `tests/helpers`  |
| MCP tool servers                      | `src/truecoder/mcp`                               | Schema bounds, the registry adapter, and the fake server in `tests/helpers` |
| JSON-RPC transport or framing         | `src/truecoder/jsonrpc`                           | Both `lsp/protocol.py` and `mcp/protocol.py`, and their transport tests |
| Context budgeting                     | `src/truecoder/agent/budget.py`                   | `context.py` assembly and `compaction.py` for long histories           |
| Token counting or launch latency      | `src/truecoder/agent/tokenizer.py`                | `TiktokenTokenCounter` in `context.py` and `run_interactive` in `agent.py` |
| Loop and stall behaviour              | `src/truecoder/agent/progress.py`                 | `_agentic_loop` in `agent.py` and the loop-detection tests             |
| Checkpoints and restore               | `src/truecoder/checkpoint`                        | `tui/checkpoints.py` and capture in `agent.py`                         |
| What a turn changed                   | `src/truecoder/checkpoint/changes.py`             | `tui/changes.py` and `turn_changes` in `agent.py`                      |
| Memory shape or scoping               | `src/truecoder/memory`                            | `builtin/memory.py`, `tui/memory.py`, projection in `context.py`       |
| Hook events or execution              | `src/truecoder/hooks`                             | `_run_hooks` and `pre_authorise` in `agent.py`                         |
| Outbound network rules                | `src/truecoder/web/policy.py`                     | `fetch.py` redirect handling and the SSRF refusal tests                |
| Diff rendering or bounds              | `src/truecoder/mutation`                          | `ToolCallCard` diff view, `styles.tcss`, preview tests                 |
| Mutation evidence                     | `src/truecoder/tools/mutation_audit.py`           | `write_file.py`, `edit_file.py`, and the schema immutability triggers  |
| Filesystem safety rules               | `src/truecoder/tools/builtin/filesystem.py`       | Every filesystem tool and its sensitive-path tests                     |
| Command classification or limits      | `src/truecoder/execution/policy.py`               | `defaults.py`, approval display, and policy unit tests                 |
| Host detection                        | `src/truecoder/execution/discovery.py`            | `selection.py`, `bootstrap.py`, and discovery integration tests        |
| Process lifecycle on POSIX            | `src/truecoder/execution/backends/posix*.py`      | The backend contract suite and POSIX integration tests                 |
| Process lifecycle on Windows          | `src/truecoder/execution/backends/windows*.py`    | Native contract and integration suites on `windows-latest`             |
| Sandbox flags or mounts               | `src/truecoder/execution/backends/container_plan` | `container_dialects.py`, `image.lock`, and the sandbox suite           |
| Audit schema or evidence              | `src/truecoder/execution/audit`                   | `schema.py` version, recovery handlers, and audit store tests          |
| Operator execution policy             | `src/truecoder/execution/configuration.py`        | Defaults, bootstrap, trusted rules, health, and configuration tests    |
| Startup wiring                        | `src/truecoder/execution/bootstrap.py`            | Health report, `prompts.py` shell guidance, and composition tests      |
| Sandbox image                         | `container/`                                      | `container/image.lock` in the same commit, then rerun the sandbox suite |

Dependencies point toward the core.
Tools never depend on the agent, client, or UI, and the UI never contains agent logic.

## Engineering scorecard

Local figures below were measured on 10 August 2026 from this working tree, on Linux with Python 3.14.3 and Docker 29.3.0.
Cross-platform behavior is exercised by the GitHub Actions matrix on Linux, macOS, and Windows.

| Signal                     |                                   Current value | Scope and interpretation                                                                                                                                     |
| -------------------------- | ----------------------------------------------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Physical source lines      |                      **38,172** across 167 files | Python under `src/truecoder`, excluding tests, the sandbox image, and generated packaging metadata.                                                           |
| Execution subsystem share  |                    **19,435 lines**, 51% of src | The execution control plane, audit store, and platform backends. Tools are 4,188 lines, the TUI is 3,892, the agent is 2,883, MCP is 957, LSP is 951, checkpoints are 735, web is 655, sessions are 518, providers are 1,377, the client is 460, JSON-RPC is 424, memory is 407, hooks are 402, mutation is 281, evaluation is 245, the CLI is 184, and planning is 146. |
| Test lines                 |                      **39,427** across 214 files | The complete Python test tree, including fakes and child-process helpers; a test-to-source ratio of roughly 1.03 to 1.                                       |
| Automated scenarios        |                      **2,215**, locally clean   | 1,965 unit, 159 integration, 41 contract, 28 end-to-end, and 22 sandbox scenarios. On Linux, 2,202 pass and 13 Windows-only scenarios skip.                     |
| Unit suite                 |                  **1,965 passing in 18.9 seconds** | Mostly pure logic with injected boundaries; platform-specific filesystem and native-boundary cases are explicitly scoped to their supported hosts.           |
| Backend contract suite     |                      **41 scenarios**, 4 adapters | One reusable contract applied to fake, POSIX, container, and Windows Job Object backends. Linux runs 31 and skips the 10 Windows-host scenarios.              |
| End-to-end suite           |                                 **28 passing** | A scripted model drives a real agent with real tools against a real workspace, asserting what changed on disk, which backend ran, and that results reached the model intact. |
| Adversarial sandbox suite  |                                 **22 passing** | Run against real Docker: host secret unreadable, read-only enforcement, network denial, capability drop, memory, PID, and CPU limits, and no container or file leaks. |
| Lint                       |                          **ruff check clean** | ruff 0.16.0 over `src`, `tests`, and `container`.                                                                                                            |
| Certified sandbox profile  |         **Linux + Docker + one pinned image** | Podman and nerdctl are refused until their dialects pass the same tests. Non-Linux hosts report `container-platform-unsupported`.                             |
| Default execution ceiling  | **120 s, 1 MiB produced, 64 KiB returned** | Requests may tighten these values and can never widen them.                                                                                                  |
| Windows support            |                **Native contract + integration** | The `windows-latest` job exercises real Job Object output, termination, descendant cleanup, process limits, and durable service timeout finalization.          |
| Fuzz scenarios             |               **10, deterministic seed** | Noisy Unicode and structural input driven through the terminal sanitizer, bounded byte stream, bounded preview, trusted-rules parser, and Windows quoter.     |
| Continuous integration     |                   **4-platform job matrix** | Linux, adversarial Linux sandbox, macOS, and Windows jobs run the suites appropriate to their native boundaries.                                               |
| Coverage percentage        |                        **Not measured** | No coverage tool is committed or installed. No coverage number is claimed until one is.                                                                       |

## Technical highlights

- **Execution is admitted, not invoked.** `AuditService.admit()` must commit a pending run and its first event to SQLite before any backend is authorized to start. If permissions, schema verification, or the write fail, admission raises and nothing runs.
- **The launch gate is real.** The POSIX supervisor forks the project leader and blocks it on a private pipe before reporting readiness, which makes the process group ID available for the durable resource identifier. `START` is sent only after audit attachment commits. The container backend does the same with create, register, then start.
- **Terminal outcomes are arbitrated, never inferred.** `asyncio.wait` can return several completed signals at once, so every completed watcher becomes a candidate claim and is ranked by fixed priority: natural exit, output limit, resource limit, cancellation, then timeout. A command that exits exactly at its deadline is never mislabelled as a timeout.
- **The first claim wins forever.** `TerminalArbiter` records one winner, and a late cancellation receives the original claim instead of replacing it.
- **One owner reads the output.** A single output task reads each raw byte once and accounts for it twice: the collector hashes and counts the exact bytes for audit evidence, and the same update produces bounded sanitized text for the interface. Digests always cover the raw stream, never the redacted preview.
- **Timeouts and safety deadlines are different things.** The request timeout is a user-visible outcome. The short internal deadline used while waiting for a broken backend is an infrastructure failure and is reported as one, and output that cannot reach EOF within it marks the evidence incomplete rather than complete.
- **Failure withholds results.** A finalization failure returns no result and leaves a nonterminal row for recovery. Incomplete cleanup records `cleanup_failed` over the real command outcome instead of reporting success.
- **Approval cannot be widened by the UI.** The safe-scope calculation is authoritative, shell requests permit approve-once only, and the approval service rejects a handler response that selects a scope outside the request's allowed set.
- **Trusted rules only restrict.** They match structured executable names after base classification, may force approval, and deny commands above their risk ceiling. They never lower risk, remove approval, widen capabilities, or increase limits.
- **No backend re-derives anything.** `PreparedExecution` carries the effective request, selected descriptor, constructed environment, and resolved shell, and `BackendRegistry.get_exact` refuses a backend whose current descriptor has drifted from the prepared one.
- **Recovery never trusts a PID.** POSIX identity includes supervisor PID, project PGID, host identity, Linux boot ID, process start ticks, protocol version, and ownership token. Container recovery requires the full immutable container ID plus exact management, run, execution, ownership, host, and protocol labels. Windows binds a Job Object handle to the owning TrueCoder process and refuses to reuse that numeric handle after a restart.
- **Discovery does not guess.** Version probes use fixed argument vectors with no shell, short timeouts, bounded output, and a minimal environment, and a mounted cgroup filesystem is never confused with a writable delegated subtree.
- **The image is pinned by content digest.** Launch uses `--pull never`, and discovery verifies platform, non-root user, and entrypoint labels against `container/image.lock` before the container backend reports itself available.
- **Cancellation is addressable from admission.** The active control entry is registered right after durable admission and before approval is awaited, so an execution can be cancelled by ID for its whole life. Pre-start cancellation records `failed_to_start` with a `cancelled_before_start` detail because the command genuinely never ran.
- **Outer cancellation waits for cleanup.** If the agent task is cancelled, the agent signals the execution's cancellation source, shields the tool task, and waits for termination, cleanup, and audit finalization before propagating.
- **`shell` is advertised conditionally.** Audit failure, discovery failure, recovery failure, or no registered backend leaves `shell` out of the model schema entirely, and the shell-specific prompt guidance exists only while the tool is registered.
- **Retention preserves the evidence model.** Startup recovers unresolved runs first, then rebuilds and atomically replaces the audit database without expired terminal rows. Nonterminal rows and immutability triggers always survive.
- **Failure is explainable without leaking internals.** Ordinary refusals return stable reason codes and bounded corrective messages; the health screen shows why shell or a backend is unavailable, while arbitrary native diagnostics stay out of model-visible results.
- **Normal failure is data, not an exception.** Nonzero exits, policy denial, approval rejection, timeout, cancellation, limit termination, and backend unavailability are all bounded successful tool payloads. Only audit and cleanup failures become sanitized infrastructure errors.

## Architecture overview and diagram

TrueCoder separates an **agent plane** that decides what to do from an **execution plane** that decides whether and how it may happen.
The agent plane owns the loop, context, tools, checkpoints, and presentation.
The execution plane owns policy, approval, evidence, isolation, and process ownership.
The shell tool is the only bridge between them, and it is a thin adapter that converts arguments and formats results.

```mermaid
flowchart TB
    User[Developer terminal]

    subgraph AgentPlane[Agent plane]
        TUI[Textual TUI<br/>transcript, tool cards, approvals]
        Agent[Agent loop<br/>turns, state, cancellation, stall detection]
        Ctx[Context builder<br/>prompt + summary + turns + plan, bounded]
        LLM[LLM client<br/>OpenAI-compatible streaming]
        Sessions[(Session store<br/>SQLite, project-scoped)]
        Checkpoints[(Checkpoints<br/>git snapshots per turn)]
        Tools[Tool registry + executor<br/>validate, prepare, approve]
        FS[Filesystem tools<br/>read, write, edit, list, glob, grep]
        Plan[Plan tool<br/>ordered checklist]
        Web[Web fetch<br/>public addresses only]
        Code[Code intelligence<br/>symbols, definitions, diagnostics]
        Shell[Shell tool<br/>model boundary only]
    end

    subgraph ExecPlane[Execution plane]
        Service[ExecutionService<br/>policy, selection, preparation]
        Runner[ExecutionRunner<br/>arbitration and finalization]
        Audit[(Audit store<br/>SQLite WAL, immutable events)]
        Approval[Approval gate<br/>fingerprints and scopes]
        Discovery[Discovery<br/>host, shells, cgroups, runtimes]

        subgraph Backends[Backends]
            Posix[POSIX local<br/>supervisor + process group]
            Container[Docker sandbox<br/>pinned, non-root, read-only]
            Win[Windows local<br/>Job Object]
        end
    end

    Provider[LLM provider<br/>any OpenAI-compatible endpoint]

    User <--> TUI
    TUI <--> Agent
    Agent --> Ctx --> LLM --> Provider
    Agent <--> Sessions
    Agent --> Tools
    Agent <--> Checkpoints
    Tools --> FS
    Tools --> Plan
    Tools --> Web
    Tools --> Code
    Tools --> Shell
    Shell -->|one ExecutionRequest| Service
    Discovery --> Service
    Service --> Runner
    Runner -->|admit, attach, finalize| Audit
    Runner --> Approval
    Approval -.->|awaited request| TUI
    Runner -->|gated start| Posix
    Runner -->|gated start| Container
    Runner -->|gated start| Win
    Web -->|pinned request| Internet[Public internet]
    Code -->|stdio JSON-RPC| Servers[Language servers]
    Posix --> Project[Project process group]
    Container --> Sandboxed[Sandboxed process]
    Win --> WindowsTree[Project Job Object]
```

### Turn lifecycle

```text
user message
→ capture a workspace checkpoint
→ compact the oldest turns if history outgrew the budget
→ start active turn
→ build context: system prompt + rolling summary + recent complete turns
  + full active turn + current plan, with oversized tool results shortened
→ call model
→ collect text and tool calls
→ prepare each call once, approve, execute, record results in order
→ stop and answer if the same calls keep returning the same results
→ call model again after every tool batch resolves
→ commit the complete pending group as one turn
→ append to the session store
```

Only complete turns enter history.
Interrupted or invalid turns are discarded rather than partially persisted.

### Execution lifecycle

```text
admission (durable)
→ policy evaluation
→ backend selection and exact preparation
→ approval
→ active registration
→ resource-gated backend start
→ supervision, arbitration, drain
→ termination and cleanup
→ one immutable terminal finalization
```

Policy denial, approval rejection, and backend unavailability all still reach one durable terminal row.
No route escapes audit.

## Technology stack

| Layer               | Technology                                  | Responsibility                                                        |
| ------------------- | ------------------------------------------- | --------------------------------------------------------------------- |
| Terminal interface  | Textual 8.2                                 | Transcript, streaming output, tool cards, approvals, and session browser |
| Agent runtime       | Python 3.10+, asyncio                       | Turn lifecycle, tool orchestration, and cancellation                   |
| Model access        | openai 2.46 async client                    | Any OpenAI-compatible endpoint, streaming and non-streaming            |
| Outbound web        | httpx 0.27+                                 | Address-pinned, bounded fetching of public pages                       |
| Code intelligence   | Language Server Protocol over stdio         | Symbols, definitions, references, and diagnostics from installed servers |
| Checkpoints         | git plumbing                                | Workspace snapshots and restore, kept out of branches and history      |
| Context budgeting   | tiktoken                                    | Token counting for turn-based context selection, loaded lazily and cached outside the temporary directory |
| Schemas             | Pydantic 2                                  | Tool arguments, strict validation, and model-facing JSON schemas       |
| Persistence         | SQLite via the standard library             | Sessions and the separate execution audit store, both WAL-journaled    |
| Storage locations   | platformdirs 4                              | User data directories outside the repository                           |
| Local execution     | POSIX process groups, cgroup v2, and Windows Job Objects | Cross-platform process-tree ownership, cancellation, cleanup, and Linux hard limits |
| Sandboxed execution | Docker with a digest-pinned image           | Filesystem, network, capability, memory, and PID isolation             |
| Configuration       | python-dotenv plus strict versioned JSON    | Provider settings and optional zero-default execution policy            |
| Lint                | ruff 0.16                                   | Static checks over source, tests, and the image entrypoint             |
| Tests               | unittest, `IsolatedAsyncioTestCase`         | Unit, contract, integration, and adversarial sandbox suites            |

## Prerequisites

- **Python 3.10 or newer.** The current development environment uses 3.14.3.
- **Git.** Project root discovery walks up to the nearest ancestor containing `.git`, and that root scopes both sessions and every filesystem tool. Git is also what backs workspace checkpoints; without it, checkpoints report themselves unavailable and everything else still works.
- **An OpenAI-compatible LLM endpoint** with a base URL, API key, and model name.
- **Linux, macOS, or Windows** for local shell execution. POSIX hosts use process groups and sessions; Windows uses a Job Object backend.
- **A language server on `PATH`** only if you want code intelligence. TrueCoder discovers pyright, pylsp, jedi, typescript-language-server, rust-analyzer, gopls, and clangd; it installs none of them, and the tools refuse with `no_server` when none matches a file.
- **Docker** only if you want the container sandbox or intend to run the sandbox test suite. Docker 29.3.0 is the version currently verified.
- **cgroup v2 with a writable delegated subtree** only if you want hard memory and PID enforcement on Linux. Without it those limits degrade to explicit best effort rather than silently pretending to be enforced.

TrueCoder runs without Docker.
The container backend simply reports itself unavailable, and `shell` continues through the supported local backend as long as audit storage and discovery are healthy.

## Local development setup

```bash
git clone https://github.com/Shivam583-hue/TrueCoder.git
cd TrueCoder

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

Create your provider configuration:

```bash
cp .env.example .env
```

Fill in `BASE_URL`, `API_KEY`, and `MODEL`, then launch:

```bash
truecoder
```

`python -m truecoder` is equivalent.

TrueCoder resolves the project root from the current working directory, so launch it from inside the repository you want it to work on.
Everything the filesystem tools can reach is rooted at that project root.

### Optional: the container sandbox

```bash
docker build -t truecoder-exec:1 container/
docker images --no-trunc --format '{{.ID}}' truecoder-exec:1
```

Write that content ID into both `reference` and `digest` in `container/image.lock`.
See [Container sandbox image](#container-sandbox-image) for verification and the rebuild policy.

### Terminal shortcuts

| Key      | Action                                            |
| -------- | ------------------------------------------------- |
| `ctrl+q` | Quit                                              |
| `ctrl+l` | Start a new chat                                  |
| `ctrl+p` | Open the session browser                          |
| `ctrl+a` | Open the workspace execution audit                |
| `ctrl+e` | Show execution and backend health                 |
| `ctrl+r` | Browse and restore workspace checkpoints          |
| `ctrl+d` | Review what this turn changed on disk             |
| `ctrl+n` | Browse and forget what the agent remembers        |
| `escape` | Cancel the in-flight response or running execution |

## Environment variables

Configuration is read from `.env` in the launch directory, or from the real environment.
Copy `.env.example` and never commit the filled-in file.

| Variable           | Required | Purpose                                                                                                             |
| ------------------ | -------- | ------------------------------------------------------------------------------------------------------------------- |
| `API_KEY`          | Yes      | Credential for the LLM endpoint. The client raises at first use if it is missing.                                    |
| `MODEL`            | Yes      | Model identifier sent with every request and shown in the TUI header.                                               |
| `BASE_URL`         | No       | OpenAI-compatible endpoint. Omit it to use the provider default.                                                    |
| `MAX_INPUT_TOKENS` | No       | Context budget for the system prompt plus selected turns. Defaults to `64000`. Lower it if your model's window is smaller. |

A suitable local `.env` starts with:

```dotenv
BASE_URL="https://your-provider.example/v1"
API_KEY="replace-with-your-key"
MODEL="your-model-id"
MAX_INPUT_TOKENS=64000
```

Note that the execution environment builder deliberately strips credential-shaped names from every child process environment.
`API_KEY`, `DATABASE_URL`, and anything matching the known token, password, private-key, or secret rules are removed from commands the agent runs, and an explicitly requested sensitive name is reported as a policy violation rather than being passed through.

### Project instructions

TrueCoder reads `AGENTS.override.md` and `AGENTS.md`, discovered from the Git project root down to the launch directory, and injects them into the system prompt.
Instructions are capped at 32 KiB.
This is the supported way to give the agent repository-specific rules.

### Optional advanced execution policy

Normal use needs no execution configuration: built-in limits, secret filtering,
backend discovery, denied container networking, and 30-day terminal-audit
retention apply automatically. The model chooses stricter per-command limits
when needed.

Operators embedding TrueCoder or managing a controlled environment can add a
versioned `execution.json` under the platform user-config directory
(`<user config>/truecoder/execution.json`). It can set deployment ceilings,
extra inherited environment names, storage paths, retention days, and container
defaults. It is strict and fail-closed: malformed JSON, unknown fields, or
invalid values make shell execution unavailable and the reason appears under
`ctrl+e`.

```json
{
  "version": 1,
  "retention": {"days": 30},
  "container": {"isolated_network": "truecoder-isolated"}
}
```

The network name must already refer to an intentionally isolated Docker
network. Without it, a command requesting container network access is rejected;
TrueCoder never silently falls back to the host network.

### Optional hooks

The same config directory may contain `hooks.json`, which runs commands you
choose around a turn:

```json
{
  "version": 1,
  "hooks": [
    {
      "name": "format",
      "event": "turn_end",
      "when": "files_changed",
      "command": ["ruff", "format", "."],
      "timeout_seconds": 60
    }
  ]
}
```

`event` is `turn_start` or `turn_end`, and `when` is `always` or
`files_changed`, where `files_changed` compares the workspace against the
pre-turn checkpoint so a formatter runs only when there is something to format.
Parsing is strict and fail-closed in the same way as `execution.json`: an
unknown field or a bad value disables every hook and reports why, rather than
running a partially understood configuration.

Because you wrote the configuration, a hook does not ask for approval. It still
goes through the execution service, so it is policy-classified, bounded by its
timeout and an output ceiling, and recorded in the durable audit. A hook that
fails is reported and never blocks the turn.

### Optional providers and browser sign-in

The same config directory may contain `providers.json`, which names where a model
comes from and, when the provider publishes one, how to sign in with a browser:

```json
{
  "version": 1,
  "providers": [
    {
      "name": "acme",
      "base_url": "https://api.acme.example/v1",
      "oauth": {
        "client_id": "your-registered-client-id",
        "authorize_url": "https://acme.example/oauth/authorize",
        "token_url": "https://acme.example/oauth/token",
        "scopes": ["models.read", "chat"]
      }
    }
  ]
}
```

With that in place, `/login` opens your browser, TrueCoder listens on a loopback
port for the single redirect, verifies the `state` it issued, exchanges the code
together with the PKCE verifier, and stores the result in `tokens.json` at mode
`0600`. `/logout` forgets it. Both endpoints must be `https`, and parsing is strict
and fail-closed in the same way as `hooks.json` and `mcp.json`.

The `client_id` is yours to supply. TrueCoder ships no registered client for any
provider, because whether a given provider permits a third-party client to use a
given account is that provider's decision and worth reading their terms for. API
key authentication needs none of this and remains the default.

### Optional MCP tool servers

The same config directory may contain `mcp.json`, which adds tools from Model
Context Protocol servers:

```json
{
  "version": 1,
  "servers": [
    {
      "name": "files",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
      "environment": {"LOG_LEVEL": "warn"},
      "startup_timeout_seconds": 30
    }
  ]
}
```

Each server is started once at launch, asked for its tools, and its tools are
registered as `mcp__<server>__<tool>`. The namespace is not decoration: a server
that offers a tool called `read_file` cannot shadow the built-in one, and two
servers offering the same name stay distinct.

A server is third-party code, so nothing it sends is trusted. Its tool schemas
are bounded before the model sees them, which caps nesting depth, property and
enum counts, and description length, drops every keyword TrueCoder does not
implement, and closes `additionalProperties` whatever the server asked for. A
schema that cannot be bounded means that tool is skipped, not that the server is
trusted to be sensible. Results are size-capped and returned to the model with a
standing note that they are third-party data and never instructions, matching how
`web_fetch` already treats a fetched page.

Every server tool requires approval, exactly like a built-in one, and goes
through the same fingerprint and audit. Parsing is strict and fail-closed like
`hooks.json`. A server that fails to start, times out, or points its working
directory outside the workspace is reported in a startup notification and
skipped; the other servers and the application start normally.

The same config directory may contain `trusted-commands.json`. Despite its
name, a rule can only make policy stricter: it can require approval for a
structured executable or deny it above a risk ceiling. It cannot waive an
existing approval, reduce risk, increase limits, or match arbitrary shell
scripts.

## Container sandbox image

The runtime never pulls at command time, so the image must already exist locally before the container backend reports itself available.

### Build and lock

```bash
docker build -t truecoder-exec:1 container/
docker images --no-trunc --format '{{.ID}}' truecoder-exec:1
```

Write the printed value into both `reference` and `digest` in `container/image.lock`.
A locally built image has no registry manifest digest, so its content ID is the pinned identity.

### Verify

```bash
docker run --rm --network none truecoder-exec:1 --version
docker run --rm --network none truecoder-exec:1 python3 -c "import os; print(os.getuid(), os.getgid(), os.getcwd())"
docker run --rm --network none --read-only --cap-drop ALL truecoder-exec:1 sh -c 'echo x > /etc/probe'
```

The first prints the entrypoint protocol version.
The second prints `65532 65532 /workspace`.
The third must fail with a read-only filesystem error.

### What the sandbox enforces

| Property         | Behavior                                                                              |
| ---------------- | ------------------------------------------------------------------------------------- |
| Identity         | Fixed non-root UID and GID 65532, with no-new-privileges active and all capabilities dropped |
| Root filesystem  | Read-only, with only approved tmpfs locations writable                                |
| Workspace        | `workspace-read` by default, `workspace-write` only when the host actually grants the sandbox user write access |
| Host filesystem  | `filesystem_mode="host"` is refused outright by the plan                              |
| Network          | Denied unless an isolated network is explicitly configured                            |
| Runtime socket   | Never mounted or visible                                                              |
| Memory and PIDs  | Hard limits defaulting to 512 MiB and 64 processes, clamped to the configured ceiling, with memory exhaustion normalized to `memory_limit` |
| CPU seconds      | Best effort through aggregate cgroup accounting in the trusted entrypoint, and advertised as best effort rather than enforced |
| Image            | Pinned by content digest, launched with `--pull never`, and verified for platform, user, and entrypoint labels |

### Rebuild policy

Rebuilding produces a new content digest.
Update `container/image.lock` in the same commit, because discovery refuses an image whose digest does not match the lock.

## Available commands and scripts

| Command                                              | Description                                                     |
| ---------------------------------------------------- | --------------------------------------------------------------- |
| `truecoder`                                          | Launch the terminal application in the current project          |
| `python -m truecoder`                                | Equivalent module entry point                                   |
| `pip install -e .`                                   | Install the package in editable mode                            |
| `python -m unittest discover -s tests -t .`          | Run every suite                                                 |
| `python -m unittest discover -s tests/unit -t .`     | Run the fast unit suite only                                    |
| `python -m unittest discover -s tests/contract -t .` | Run the backend contract suite                                  |
| `python -m unittest discover -s tests/integration -t .` | Run real-process, SQLite, and TUI integration scenarios      |
| `python -m unittest discover -s tests/e2e -t .`      | Run the end-to-end task suite                                   |
| `truecoder -p "..."`                                 | Run one prompt without the interface                            |
| `truecoder -p "..." --autonomy edit`                 | Allow file changes and medium-risk commands unattended          |
| `truecoder --eval`                                   | Score the agent on the shipped tasks                            |
| `/models` in the composer                            | Choose which model answers                                      |
| `/login` in the composer                             | Authorise the current provider in your browser                  |
| `python -m unittest discover -s tests/sandbox -t .`  | Run the adversarial Docker sandbox suite                        |
| `ruff check src tests container`                     | Lint source, tests, and the image entrypoint                    |
| `docker build -t truecoder-exec:1 container/`        | Build the execution sandbox image                               |

## Testing

The suite is written in plain `unittest` with `IsolatedAsyncioTestCase`, so no test runner beyond the standard library is required.

```bash
python -m unittest discover -s tests -t .
```

Current inventory: **1,711 scenarios**.
In the local Linux verification, 1,698 pass and 13 Windows-only scenarios skip;
the Windows job runs those native contract and integration cases on
`windows-latest`.

The four suites prove different classes of guarantee, and they are kept separate on purpose.

### Unit, 1,515 scenarios

Mostly pure logic behind injected boundaries, plus narrowly scoped platform fixtures for filesystem and native-boundary behavior.
`DiscoveryIO` is modeled rather than measured, so these scenarios describe Linux, macOS, Windows, and unknown hosts without depending on the machine running them.
Coverage includes policy classification and limit tightening, environment allowlists and secret removal, bounded output with property-style chunk-boundary and Unicode-split variation, capability matching, lifecycle transitions, terminal claim arbitration, result conversion, audit models, codecs, permissions, recovery, every filesystem tool's security boundary, the shell adapter's argument contract, agent state, turn selection, and startup composition. Newer areas are covered the same way: unified diff generation and its bounds, URL and public-address policy with the SSRF refusals, JSON-RPC framing and the language-server client against a real child process, checkpoint capture and restore against real repositories, tool-result shortening, rolling compaction, and loop detection.

### Contract, 41 scenarios

One reusable backend contract applied to four adapters: fake, POSIX, container, and Windows Job Object.
It encodes the invariants that make backend ownership safe, including exact resource identity on successful start, cleanup before raising on a failed registration, idempotent terminate and wait, a single output owner reaching end of stream, and nonzero exit treated as ordinary backend data.
A backend must pass this suite before the execution service can register it.
Host-specific adapters skip only when their operating-system boundary is unavailable; the CI matrix runs POSIX on Linux and macOS and the native Job Object contract on Windows.

### Integration, 133 scenarios

Real processes, real SQLite, and the real Textual application.
This suite covers the POSIX supervisor's gate and lifetime pipe, termination escalation and first-reason preservation, environment filtering observed from inside a child, recovery against a live exact resource, audit routes for every terminal outcome, host discovery on the actual machine, the session store and manager, the TUI, and the shell tool driven through the agent boundary including outer cancellation.
On Windows it also exercises real Job Object descendant cleanup and process
limits, plus a full timeout through the execution service and durable audit.
It also covers the reviewable-mutation diff in an approval card, the evolving plan card, and the checkpoint browser and its restore confirmation. It also asserts the interface lifecycle gate: a card evolves only from typed stages, stop cancels one execution by id rather than the turn, a second stop does not cancel twice, a rapid completion never leaves a card running, and shutdown resolves an awaited approval and cancels every active execution.

### Sandbox, 22 scenarios

Adversarial checks against real Docker.
These are the claims the sandbox makes, tested rather than asserted: a host secret outside the workspace is unreadable, `workspace-read` cannot mutate the host tree, `workspace-write` is refused when the host denies it, the root filesystem is read-only, only approved tmpfs locations are writable, network denial blocks a real external canary, no runtime socket is visible, every capability is dropped, no-new-privileges is active, memory exhaustion normalizes to `memory_limit`, the PID limit blocks fork growth, a CPU-bound process is stopped at its aggregate budget, stdout and stderr stay separate and raw, the private environment file never survives the run, a signal-ignoring command is still removed, a registration failure removes the stopped container, the container is created stopped before registration, and recovery removes only the exact labeled container.

This suite requires Docker and the locally built image whose digest matches `container/image.lock`.

### Continuous integration

`.github/workflows/tests.yml` runs lint plus the unit, contract, and integration suites on Linux; builds and locks the sandbox image before running the adversarial suite; and runs the unit, contract, and integration suites on both `macos-latest` and `windows-latest`.

## Runtime data and storage

TrueCoder keeps its state out of your repository, with one deliberate exception.
Checkpoints are git objects and refs, so they live inside `.git` where the content they snapshot already is; they never appear on a branch, in your history, or in a push.

| Store               | Location                                                    | Contents                                                           |
| ------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------ |
| Sessions            | `<user data dir>/truecoder/sessions.sqlite3`                | Completed turns, scoped by canonical project root                  |
| Execution audit     | `<user data dir>/truecoder/audit.sqlite3`                   | Runs, immutable event log, resource identities, terminal outcomes  |
| Mutation audit      | `<user data dir>/truecoder/mutations.sqlite3`               | One immutable record per applied write or edit, with both digests  |
| Memory              | `<user data dir>/truecoder/memory.sqlite3`                  | Durable notes, scoped by canonical workspace identity              |
| Hooks               | `<user config dir>/truecoder/hooks.json`                    | Optional user-configured commands, strict and versioned            |
| MCP servers         | `<user config dir>/truecoder/mcp.json`                      | Optional third-party tool servers, strict and versioned            |
| Providers           | `<user config dir>/truecoder/providers.json`                | Optional base URLs and OAuth clients, strict and versioned         |
| Model selection     | `<user config dir>/truecoder/settings.json`                 | The model chosen with `/models`, remembered across restarts        |
| Authorisation       | `<user config dir>/truecoder/tokens.json`                   | OAuth tokens, one per provider, private to your user               |
| Model catalog       | `<user cache dir>/truecoder/models.json`                    | The provider's model list, refetched after six hours               |
| Tokenizer           | `<user cache dir>/truecoder/tokenizers/`                    | The token encoding, downloaded once and reused for every launch    |
| Checkpoints         | `refs/truecoder/checkpoints/*` inside the repository        | Workspace snapshots as git objects, pruned to the newest 25        |
| Execution policy    | `<user config dir>/truecoder/execution.json`                | Optional operator ceilings and backend settings                    |
| Trusted rules       | `<user config dir>/truecoder/trusted-commands.json`         | Optional executable-specific restrictions                          |
| Project instructions | `AGENTS.md` and `AGENTS.override.md` in the repository      | Read only, never written                                           |

The user data directory is resolved by platformdirs, so it follows the operating system's convention.
The two cache entries are the only ones you can delete freely; they are rebuilt on demand, and they live in the cache directory rather than the system temporary directory so that a reboot does not make you pay for them again.

Every database uses WAL journaling and full synchronous durability.
The mutation audit is a separate database with its own schema version rather than a table inside the execution audit, because a file change has no lifecycle to arbitrate and no resource to recover, and because bumping the execution audit's schema version would make an existing installation report an unsupported database and lose shell execution entirely.
The audit directory and files are private by construction: POSIX uses directory mode `0700` and file mode `0600` including SQLite sidecars, and Windows removes inherited ACLs and grants access only to the current user and LocalSystem.
Failing to establish those restrictions makes audit storage unavailable rather than silently weakening it, which in turn removes `shell` from the tool schema.
On startup, nonterminal runs are recovered before terminal evidence older than
the configured retention window is compacted. The compaction atomically
replaces a verified database and never removes nonterminal evidence.

Sessions are isolated by canonical project root.
One repository cannot list, resume, rename, or delete another repository's sessions.
Empty sessions are temporary placeholders and are removed automatically when you create another session, switch away, or close the application.

## Known limitations

- **Only the Docker dialect is certified.** Podman and nerdctl are refused by both the plan and capability derivation until their dialects pass the same contract and sandbox suites. Non-Linux hosts report `container-platform-unsupported`.
- **Container CPU limits are best effort.** The trusted entrypoint monitors aggregate cgroup CPU accounting and the sandbox suite verifies termination of a busy process, but this userspace monitor is still advertised as best effort rather than kernel-enforced isolation.
- **Local execution is not isolation.** POSIX process groups and Windows Job Objects provide reliable process lifecycle management, and Linux can add hard memory and PID limits through delegated cgroup v2. Local backends do not provide filesystem or network isolation and report those capabilities as unsupported. Use the container backend when isolation matters.
- **Windows restart recovery fails closed.** A Win32 Job Object handle is valid only in the TrueCoder process that owns it. `KILL_ON_JOB_CLOSE` terminates descendants when that process disappears, and startup recovery refuses to reuse the persisted numeric handle rather than risking an unrelated resource.
- **macOS recovery fails closed.** After a restart, a live macOS resource whose exact ownership cannot be proven with the available facts is refused rather than assumed. This is the intended tradeoff, but it means some macOS resources need manual cleanup.
- **macOS cannot bound a process tree.** `RLIMIT_NPROC` is per-user on macOS, so applying it as a tree limit could exhaust the login session instead of the command. macOS therefore never applies it and reports `process_limits` as `unsupported`. Use the container backend on Linux when process-count enforcement matters.
- **macOS and Docker Desktop are not certified.** The container backend stays gated to the Linux Docker profile, so macOS reports `container-platform-unsupported`. Extending certification needs the adversarial sandbox suite to pass on a macOS runner, which has not happened.
- **cgroup limits depend on delegation.** Hard limits use only controllers that discovery found both available and enabled in a writable delegated subtree. Elsewhere they degrade to explicit best-effort rlimits.
- **Approval grants are in-memory only.** Session and workspace grants live in the running application and do not survive a restart. Rejections are never remembered.
- **A reused approval grant shows no diff.** Session and workspace grants for `write_file` and `edit_file` skip the approval interaction entirely, so a later call under the same grant applies without a rendered review. The fingerprint still covers the canonical arguments, so an identical fingerprint means an identical change; approve once when you want to see each diff.
- **Mutation evidence is best effort, unlike execution evidence.** An execution withholds its result when audit finalization fails, but a file replacement is already durable by the time it could be recorded, so failing the call would report an outcome that did not happen. A recording failure therefore increments a counter instead of failing the tool.
- **Code intelligence needs a language server installed.** `find_symbol` and friends refuse with `no_server` when nothing on `PATH` handles the file's language. TrueCoder discovers pyright, pylsp, jedi, typescript-language-server, rust-analyzer, gopls, and clangd; it never installs one for you.
- **Code intelligence is read-only in this version.** Rename, code actions, formatting, and workspace edits are deliberately absent, so the language server can never change a file behind the mutation review path.
- **Hooks run on the host, like a git hook.** A hook exists to run your own toolchain, and a formatter or linter does not exist inside the digest-pinned sandbox, so hooks use the local backend with host filesystem and network access. They remain bounded by a timeout and output ceiling, policy-classified, and written to the durable audit, which is more control than a git hook has, but a hook is still a command you have chosen to trust.
- **Memory persists until you delete it.** Notes survive restarts and are sent to the model before every reply in that workspace. `ctrl+n` shows every note and removes any of them. Nothing is shared between workspaces, and nothing is written into your repository.
- **Checkpoints require a git repository.** A workspace that is not a git repository, or a machine without git, reports checkpoints as unavailable rather than falling back to something weaker that looks the same.
- **Checkpoints are byte exact.** Snapshot and restore run with git's line-ending conversion disabled, so a restore returns the exact bytes that were there rather than the platform-normalised version of them.
- **A turn diff shows text, not everything.** Binary files and files over 1 MiB are named with their change kind but not diffed, at most 50 changed files are listed, and rendering is capped so one enormous turn cannot stall the interface. Paths ignored by `.gitignore` are neither checkpointed nor compared.
- **Restore covers tracked content only.** Files the agent created without staging them survive a restore, because removing untracked files would risk deleting your own scratch work. Anything ignored by `.gitignore` is neither captured nor restored.
- **Restore rewinds staging too.** A restore returns the index to the checkpoint, so work you staged after the checkpoint is reverted with everything else. The safety checkpoint taken immediately beforehand is how you get it back.
- **Loop detection compares calls, not intent.** A model that varies its arguments trivially on every attempt keeps its tools until the `max_iterations` cap. The detector deliberately errs toward letting real work continue, because interrupting genuine progress is worse than paying for a few extra turns.
- **Compaction is not persisted.** A rolling summary lives in memory for the running session. Resuming a stored session replays its turns and re-compacts from scratch rather than restoring the previous summary.
- **`web_fetch` reaches only public addresses, on purpose.** Fetching `http://localhost:3000` from your own dev server is refused, because the same rule is what stops a redirect chain reaching cloud metadata. There is no opt-out; use `shell` with `curl` when you genuinely mean to reach a local service.
- **Fetched pages are still model input.** The untrusted-content notice and prompt guidance reduce the risk that a page instructs the agent, they do not eliminate it. `web_fetch` requires approval for that reason, so you see the URL before it is read.
- **`web_fetch` renders no JavaScript.** It returns the server's HTML as text, so single-page applications that assemble their content in the browser come back nearly empty.
- **`edit_file` matches line endings literally.** `old_text` containing a newline will not match a CRLF file, which mostly affects Windows checkouts. This is existing behavior rather than a diff-rendering problem: the tool reports `text_not_found` instead of editing the wrong thing, and single-line replacements are unaffected.
- **Mutation evidence has no retention policy yet.** The execution audit compacts expired terminal evidence on startup; the mutation store only grows. Records are small, but nothing prunes them.
- **The task plan is scratch, not a record.** The plan lives in memory for the active task and is cleared by a new chat or a session switch. Restoring a session brings back its turns but not its plan, and historical `update_plan` calls are deliberately not redrawn as tool cards so the transcript never implies a plan the model no longer has.
- **No coverage measurement is committed.** No coverage tool is installed or configured, so this README claims no coverage percentage.
- **No formatter configuration is committed.** `ruff check` is clean, but the repository pins no `[tool.ruff]` section, so `ruff format` would apply defaults that disagree with the codebase's existing line width. Either commit a configuration matching the current style or accept a one-time reformat, but do not leave it ambiguous.
- **The sandbox suite needs a matching local image.** A rebuilt image with an unlocked digest makes the container backend unavailable, which is correct behavior but easy to trip over during development.
- **Single provider shape.** The client targets OpenAI-compatible chat completions. Other provider protocols would need a new client behind the same internal event types.

## Contributing

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the authoritative design reference.
It ends with the invariant list this codebase is built to hold, and a change that breaks one of those invariants needs a deliberate argument rather than a passing test.

The rules that matter most:

- Dependencies point toward the core. Tools must not depend on the agent, client, or UI, and the UI must not contain agent logic.
- Provider-specific behavior stays inside the client.
- A new backend must pass the shared contract suite before it can be registered.
- Anything that changes what a command may do must be reflected in policy, the approval fingerprint, and the audit record together.
- Rebuilding the sandbox image and updating `container/image.lock` belong in the same commit.

## Security

Please do not report suspected vulnerabilities through public GitHub issues.

Report them privately by email at
[shivamshivamshivam456@gmail.com](mailto:shivamshivamshivam456@gmail.com).

Include a description of the issue, reproduction steps, affected component,
and any suggested mitigation. I will acknowledge reports as soon as practical.

Please note the intended trust boundary when deciding whether something is a vulnerability.
The container backend is a security boundary and is tested as one.
The POSIX and Windows local backends are process-management boundaries, not isolation boundaries, and policy classification improves safety and approval quality without being an isolation mechanism.

## License

This repository does not currently include a license file, so default copyright applies and no usage rights are granted.
