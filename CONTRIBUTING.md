# Contributing to TrueCoder

Thank you for helping improve TrueCoder. Contributions of code, tests,
documentation, bug reports, and design feedback are welcome.

TrueCoder treats model access, filesystem mutation, and command execution as
explicit boundaries. A change can pass its immediate tests and still weaken one
of those boundaries, so begin by reading the relevant design notes and finish by
testing the guarantee your change affects.

## Before you start

- This repository does not currently include a license file. Default copyright
  applies and no general reuse rights are granted; this contribution guide does
  not change that. See the README's [license notice](README.md#license).
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

TrueCoder requires Python 3.10 or newer and Git. Follow the README's
[local setup](README.md#local-development-setup) to clone the project, create a
virtual environment, and install the package. Then install the lint version used
by CI:

```bash
python -m pip install ruff==0.16.0
```

In PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

Docker is optional unless you are changing the container backend or running the
sandbox suite. Copy `.env.example` to `.env` only when a manual provider test
needs it, and never commit the populated file. Most automated tests use fakes
and do not require a real credential.

See the README for the full list of [prerequisites](README.md#prerequisites),
[environment variables](README.md#environment-variables), and
[available commands](README.md#available-commands-and-scripts).

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
digest matches `container/image.lock`. Follow
[`container/README.build.md`](container/README.build.md) to build, lock, and
verify it, then run:

```bash
python -m unittest discover -s tests/sandbox -t .
```

The container backend is a security boundary. Changes to its image, launch
arguments, resource identity, filesystem access, network access, cleanup, or
limits need tests that exercise the real Docker boundary.

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
