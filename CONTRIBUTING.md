# Contributing to TrueCoder

Thank you for helping improve TrueCoder. Contributions of code, tests,
documentation, bug reports, and design feedback are welcome.

TrueCoder treats model access, filesystem mutation, and command execution as
explicit boundaries. A change can pass its immediate tests and still weaken one
of those boundaries, so begin by reading the relevant design notes and finish by
testing the guarantee your change affects.

## Before you start

- Read and follow the project [Code of Conduct](CODE_OF_CONDUCT.md) in every
  project space and interaction.
- Contributions are accepted under the repository's [MIT License](LICENSE).
  Submit only work you have the right to license under those terms.
- Read the [architecture reference](docs/ARCHITECTURE.md), especially its
  invariant list, before changing execution, approval, audit, credential, or
  filesystem behavior. A change that breaks an invariant needs a deliberate
  design argument rather than only a passing test.
- Review the [repository map](README.md#repository-structure) to find the owning
  package and its neighboring tests.
- Search existing issues and pull requests before starting a large change. For
  behavior or architecture changes, describe the user problem and proposed
  boundary before investing in an implementation.
- Report suspected vulnerabilities privately, following the
  [security policy](README.md#security). Do not open a public issue for them.

## Development setup

TrueCoder requires Python 3.10 or newer and Git. Docker is optional unless you
are changing the container backend or running the sandbox suite.

On Linux or macOS:

```bash
git clone https://github.com/Shivam583-hue/TrueCoder.git
cd TrueCoder
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install ruff==0.16.0
```

On Windows PowerShell:

```powershell
git clone https://github.com/Shivam583-hue/TrueCoder.git
Set-Location TrueCoder
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install ruff==0.16.0
```

The editable install provides both `truecoder` and `python -m truecoder`. To
exercise the interface manually, copy the provider template and fill in
`MODEL`; credentials can remain empty if you intend to connect through
`/models`:

```bash
cp .env.example .env
truecoder
```

Never commit the populated `.env`. Most automated tests use fakes and do not
require a real credential.

See the README for the full list of [prerequisites](README.md#prerequisites),
[environment variables](README.md#environment-variables), and runtime
[commands](README.md#available-commands-and-scripts).

## Making a change

Keep each change focused enough that its behavior and safety impact are clear in
review. Add or update the narrowest test that demonstrates the intended result,
then run the broader suite for the boundary you touched.

The rules that matter most are:

- Dependencies point toward the core. Tools must not depend on the agent,
  client, or UI, and the UI must not contain agent logic.
- Provider configuration, credentials, and model discovery stay in
  `providers`; wire-format translation stays in `client`.
- A new backend must pass the shared contract suite before it can be registered.
- Anything that changes what a command may do must be reflected in policy, the
  approval fingerprint, and the audit record together.
- Rebuilding the sandbox image and updating `container/image.lock` belong in
  the same commit.

Additional expectations:

- Preserve explicit limits on output, scans, schemas, history, and other data
  that can grow with external input.
- Keep platform behavior honest. Do not claim support for a capability that a
  backend cannot enforce, and do not make POSIX assumptions in shared paths.
- Keep provider identities and transports explicit. A gateway model and a
  direct-provider model may share a model name without sharing credentials,
  endpoints, or wire behavior.
- Treat errors and refusals as part of the public experience: keep them bounded,
  actionable, and free of credentials or untrusted internal details.
- Avoid unrelated cleanup in the same pull request. It makes behavioral and
  security review harder.

### Where to make common changes

| Change                                | Primary location                                  | Usually also check                                                     |
| ------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------- |
| Terminal UI, transcript, or approvals | `src/truecoder/tui`                               | `styles.tcss`, agent events, and the TUI integration tests             |
| Agent loop or turn lifecycle          | `src/truecoder/agent/agent.py` and `state.py`     | Context builder, session codec, and unit agent tests                   |
| Agent modes or approval bypass        | `src/truecoder/agent/mode.py` and `agent.py`      | TUI mode controls, CLI wiring, delegation, and mode enforcement tests  |
| Providers, credentials, or models     | `src/truecoder/providers`                         | `client`, `tui/credentials.py`, and provider tests                     |
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
| Sandbox flags or mounts               | `src/truecoder/execution/backends/container_plan.py` | `container_dialects.py`, `image.lock`, and the sandbox suite         |
| Audit schema or evidence              | `src/truecoder/execution/audit`                   | `schema.py` version, recovery handlers, and audit store tests          |
| Operator execution policy             | `src/truecoder/execution/configuration.py`        | Defaults, bootstrap, trusted rules, health, and configuration tests    |
| Startup wiring                        | `src/truecoder/execution/bootstrap.py`            | Health report, `prompts.py` shell guidance, and composition tests      |
| Sandbox image                         | `container/`                                      | `container/image.lock` in the same commit, then rerun the sandbox suite |

## Tests and checks

The project uses the standard-library `unittest` runner and
`IsolatedAsyncioTestCase`. Run a focused test while iterating, then the relevant
suite before submitting.

```bash
# One module
python -m unittest tests.unit.providers.test_providers

# Fast logic and boundary tests
python -m unittest discover -s tests/unit -t .

# Shared execution-backend contract
python -m unittest discover -s tests/contract -t .

# Real processes, SQLite, provider flows, and the Textual UI
python -m unittest discover -s tests/integration -t .

# Full agent behavior in throwaway workspaces
python -m unittest discover -s tests/e2e -t .

# Every suite, including sandbox tests when their prerequisites are available
python -m unittest discover -s tests -t .

# Static checks used by CI
ruff check src tests container
```

Choose tests according to the change:

| Change | Minimum relevant coverage |
| --- | --- |
| Pure domain logic, parsing, serialization, or bounds | Unit suite |
| Execution backend or lifecycle | Unit and contract suites, plus that platform's integration tests |
| TUI, credentials, sessions, SQLite, or real-process behavior | Unit and integration suites |
| Agent orchestration or tool behavior | Unit and end-to-end suites |
| Container policy, image, or entrypoint | Unit, contract, and sandbox suites |

The Linux CI job runs Ruff plus the unit, contract, integration, and end-to-end
suites. The macOS and Windows jobs run those same four test suites on their
native backends. A separate Linux job builds and locks the Docker image before
running the sandbox suite. Platform-specific cases may skip locally, so use the
CI result as part of the cross-platform verification rather than weakening a
test to make the skip go away.

### What each suite proves

The five suites prove different classes of guarantee, and they are kept
separate on purpose.

#### Unit

Mostly pure logic behind injected boundaries, plus narrowly scoped platform
fixtures for filesystem and native-boundary behavior. `DiscoveryIO` is modeled
rather than measured, so these scenarios describe Linux, macOS, Windows, and
unknown hosts without depending on the machine running them.

Coverage includes policy classification and limit tightening, environment
allowlists and secret removal, bounded output, capability matching, lifecycle
transitions, terminal claim arbitration, result conversion, audit models,
codecs, permissions, recovery, filesystem security, agent state, turn
selection, startup composition, diff generation, URL policy, JSON-RPC framing,
checkpoint behavior, result shortening, compaction, and loop detection.

#### Contract

One reusable backend contract is applied to the fake, POSIX, container, and
Windows Job Object adapters. It encodes the invariants that make backend
ownership safe, including exact resource identity on successful start, cleanup
before raising on failed registration, idempotent termination and waiting, one
output owner reaching end of stream, and nonzero exit treated as ordinary
backend data. A backend must pass this suite before it can be registered.

#### Integration

Real processes, real SQLite databases, and the real Textual application. These
tests cover process gates and cleanup, audit routes, host discovery, provider
authorization, sessions, the TUI, and the shell tool through the agent boundary.
Windows CI also exercises real Job Object descendant cleanup and limits; POSIX
CI exercises its native supervisor and process lifecycle.

#### End-to-end

A scripted model drives a real agent with real tools against throwaway
workspaces. These scenarios assert outcomes on disk, the backend that ran, and
that tool results reached the model intact without coupling success to one exact
sequence of calls.

#### Sandbox

Adversarial checks run against real Docker. They verify the host and root
filesystem boundaries, workspace modes, network denial, capability removal,
no-new-privileges, resource limits, raw output separation, private environment
handling, cleanup, stopped-before-registration launch, and exact-identity
recovery.

### Sandbox changes

The sandbox suite is adversarial and requires Docker plus a local image whose
digest matches `container/image.lock`. Build the image and obtain its content
ID with:

```bash
docker build -t truecoder-exec:1 container/
docker images --no-trunc --format '{{.ID}}' truecoder-exec:1
```

Write the printed content ID into both `reference` and `digest` in
`container/image.lock`, then perform the basic image checks:

```bash
docker run --rm --network none truecoder-exec:1 --version
docker run --rm --network none truecoder-exec:1 python3 -c "import os; print(os.getuid(), os.getgid(), os.getcwd())"
docker run --rm --network none --read-only --cap-drop ALL truecoder-exec:1 sh -c 'echo x > /etc/probe'
```

The first command prints the entrypoint protocol version, the second prints
`65532 65532 /workspace`, and the third must fail with a read-only filesystem
error. See [`container/README.build.md`](container/README.build.md) for the
authoritative image contract and troubleshooting guidance. Finally, run:

```bash
python -m unittest discover -s tests/sandbox -t .
```

The container backend is a security boundary. Changes to its image, launch
arguments, resource identity, filesystem access, network access, cleanup, or
limits need tests that exercise the real Docker boundary. Rebuilding produces a
new content digest, so commit the corresponding `container/image.lock` update
with the image change; discovery deliberately refuses a digest mismatch.

## Code and documentation style

- Follow the surrounding Python style and keep interfaces typed.
- Run `ruff check src tests container`. The repository intentionally has no
  formatter configuration yet, so do not apply a repository-wide automatic
  format as incidental cleanup.
- Prefer deterministic async tests with injected clocks, I/O, or platform facts
  when the behavior does not require a real boundary.
- Name tests after observable behavior. Regression tests should fail for the
  original defect and explain the guarantee being restored.
- Update README usage guidance when user-facing commands, configuration, or
  behavior changes. Update `docs/ARCHITECTURE.md` when a responsibility,
  dependency direction, data flow, or invariant changes.
- Keep commands and links relative where possible so documentation works both
  on GitHub and in a local checkout.

## Commits and pull requests

The project history uses Conventional Commit-style subjects, for example:

```text
feat(providers): add direct provider discovery
fix(tui): keep model identity consistent
test(client): isolate transport settings
docs: clarify contributor workflow
```

Use an imperative, concise subject and select the smallest useful scope. Keep
generated files, credentials, local databases, virtual environments, and filled
`.env` files out of commits.

When TrueCoder creates a commit, its system guidance asks it to append
`Co-authored-by: TrueCoder-agent <truecoder39@gmail.com>` so the agent's work is
visible in repository history. A user may explicitly opt out; honor that choice
and do not rewrite an existing commit solely to add the trailer.

A pull request should include:

- the user-visible problem or engineering guarantee being addressed;
- the chosen behavior and meaningful tradeoffs;
- tests added or changed, plus the exact checks run;
- platform limitations or unverified paths;
- screenshots or a short recording for material TUI changes; and
- migration, compatibility, security, or audit implications when applicable.

Before requesting review, confirm that the diff contains only intended files,
that documentation links resolve, and that every relevant test suite passes.
If a check cannot be run locally, state why and identify the CI job or reviewer
environment expected to cover it.
