# Changelog

All notable changes to TrueCoder will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The project has no tagged releases yet, so the current baseline remains under
Unreleased until the first release is cut.

## [Unreleased]

### Added

- Terminal-native coding-agent interface with streaming responses, searchable
  slash commands, inline approvals, cancellation, and persistent sessions.
- Fifteen built-in tools for filesystem work, shell execution, web access,
  language-server intelligence, planning, memory, and bounded delegation.
- Direct OpenAI access with ChatGPT browser and device authorization or API
  keys, native Anthropic and Gemini transports, and explicit Models.dev
  provider discovery with per-provider credentials.
- Policy-classified, approval-fingerprinted command execution with durable
  SQLite audit evidence, bounded output, crash recovery, and native POSIX and
  Windows process supervision.
- Optional digest-pinned Docker sandbox with a non-root identity, read-only
  root filesystem, denied network, dropped capabilities, and adversarial tests.
- Reviewable atomic file mutations, mutation evidence, git-backed workspace
  checkpoints, turn-level change review, and undoable restores.
- Project-scoped durable memory, rolling context compaction, task planning,
  user-configured hooks, and approval-gated MCP tool servers.
- Build, Plan, and Full Access agent modes, with `shift+tab` switching, visible
  per-turn mode labels, structural read-only enforcement in Plan, a one-time
  Full Access confirmation, and matching `--mode` support for headless runs.
- Contributor documentation covering setup, architecture boundaries, test
  selection, common change locations, sandbox verification, and review
  expectations.
- MIT License.

### Changed

- Agent-authored Git commits now include
  `Co-authored-by: TrueCoder-agent <truecoder39@gmail.com>` unless the user
  explicitly opts out.
- Unified provider connection and model selection under `/models`: the picker
  now shows connected models and popular providers together, opens the complete
  provider directory with `ctrl+a`, and waits until authentication and model
  choice are complete before changing the active session.
- Reorganized the README around a concise feature overview and user-facing
  runtime guidance, with the complete feature catalog retained separately.

### Fixed

- Kept provider identity and displayed model names consistent across the model
  picker, composer, and request transport.
- Made deferred credential focus and approval interactions safe across slower
  Windows and macOS Textual event loops.
