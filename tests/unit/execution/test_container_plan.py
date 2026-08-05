from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from tests.helpers.platforms import requires_posix_permissions
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.container_dialects import (
    FORBIDDEN_ARGUMENTS,
    docker_create_argv,
    docker_remove_argv,
    docker_stop_argv,
    parse_container_id,
    parse_docker_inspect,
)
from truecoder.execution.backends.container_identity import (
    create_container_resource,
    read_facts,
    verify_container_identity,
)
from truecoder.execution.backends.container_models import (
    CONTAINER_WORKSPACE,
    LABEL_MANAGED,
    LABEL_OWNERSHIP_TOKEN,
    ContainerBackendFacts,
    ContainerImage,
    ContainerInspection,
    ContainerLabels,
    ContainerMount,
    ContainerSecurityProfile,
    ContainerTmpfs,
)
from truecoder.execution.backends.container_plan import (
    ContainerLaunchConfig,
    build_container_plan,
    build_env_file_content,
)
from truecoder.execution.backends.models import BackendDescriptor, ContainerRuntimeInfo
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import BackendStartError
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)
from truecoder.execution.preparation import PreparedExecution

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
TOKEN = "t" * 64
ROOT = Path.cwd().resolve()


def image(digest: str = DIGEST) -> ContainerImage:
    return ContainerImage(
        reference=digest,
        digest=digest,
        platform="linux/amd64",
        user="65532:65532",
        entrypoint_version="1",
    )


def runtime_info() -> ContainerRuntimeInfo:
    return ContainerRuntimeInfo(
        name="docker",
        executable=ROOT / "docker",
        client_version="29.3.0",
        server_version="29.3.0",
        daemon_reachable=True,
        rootless="no",
    )


def facts(**overrides) -> ContainerBackendFacts:
    values = {
        "runtime": "docker",
        "runtime_version": "29.3.0",
        "image": image(),
        "supports_read_only_root": True,
        "supports_bind_mounts": True,
        "supports_tmpfs": True,
        "supports_capability_drop": True,
        "supports_no_new_privileges": True,
        "supports_none_network": True,
        "supports_memory_limit": True,
        "supports_pids_limit": True,
        "cpu_enforcement": "best_effort",
        "dialect_implemented": True,
        "daemon_reachable": True,
        "platform_supported": True,
    }
    values.update(overrides)
    return ContainerBackendFacts(**values)


def descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        name="container",
        available=True,
        capabilities=facts().capabilities(),
        version="29.3.0",
        runtime=runtime_info(),
    )


def workspace() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="tc-plan-")).resolve()
    os.chmod(directory, 0o755)
    return directory


def cleanup_tree(case: unittest.TestCase, directory: Path) -> None:
    case.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))


def request(
    root: Path,
    *,
    mode="exec",
    argv=("python3", "-V"),
    script=None,
    filesystem_mode="workspace-read",
    network_access=False,
    working_directory: Path | None = None,
    memory_bytes: int | None = None,
    max_processes: int | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        mode=mode,
        argv=argv if mode == "exec" else None,
        script=script,
        working_directory=working_directory or root,
        limits=ExecutionLimits(
            timeout_seconds=30,
            max_output_bytes=1 << 20,
            max_return_bytes=4096,
            memory_bytes=memory_bytes,
            max_processes=max_processes,
            termination_grace_seconds=1,
        ),
        network_access=network_access,
        filesystem_mode=filesystem_mode,
    )


def start_context(root: Path) -> BackendStartContext:
    return BackendStartContext(
        execution=ExecutionContext(
            execution_id="exec-plan-01",
            tool_call_id="call-plan-01",
            session_id="session",
            turn_id="turn",
            workspace_id="workspace",
            project_root=root,
            launched_at_utc=datetime.now(UTC),
        ),
        audit_run_id="run_plan_01",
    )


def prepared(root: Path, execution_request: ExecutionRequest) -> PreparedExecution:
    return PreparedExecution(
        request=execution_request,
        backend=descriptor(),
        environment=construct_environment(
            platform="posix",
            inherited={},
            requested=execution_request.environment,
        ),
        resolved_shell="posix" if execution_request.mode == "shell" else None,
    )


def plan_for(root: Path, execution_request: ExecutionRequest, **overrides):
    return build_container_plan(
        prepared(root, execution_request),
        start_context(root),
        descriptor(),
        overrides.pop("config", ContainerLaunchConfig(image=image())),
        ownership_token=overrides.pop("ownership_token", TOKEN),
        env_file=overrides.pop("env_file", None),
    )


class PlanMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = workspace()
        cleanup_tree(self, self.root)

    def test_workspace_read_produces_a_read_only_mount(self):
        plan = plan_for(self.root, request(self.root))

        mount = plan.workspace_mount
        self.assertEqual(mount.source, self.root)
        self.assertEqual(mount.target, CONTAINER_WORKSPACE)
        self.assertTrue(mount.read_only)

    def test_workspace_write_produces_a_writable_mount(self):
        os.chmod(self.root, 0o777)

        plan = plan_for(
            self.root,
            request(self.root, filesystem_mode="workspace-write"),
        )

        self.assertFalse(plan.workspace_mount.read_only)

    @requires_posix_permissions
    def test_workspace_write_is_refused_when_the_sandbox_user_cannot_write(self):
        os.chmod(self.root, 0o755)

        with self.assertRaises(BackendStartError):
            plan_for(
                self.root,
                request(self.root, filesystem_mode="workspace-write"),
            )

    def test_nested_working_directory_maps_under_the_workspace(self):
        nested = self.root / "tests" / "unit"
        nested.mkdir(parents=True)

        plan = plan_for(
            self.root,
            request(self.root, working_directory=nested),
        )

        self.assertEqual(plan.workdir, PurePosixPath("/workspace/tests/unit"))

    def test_a_working_directory_outside_the_workspace_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="tc-outside-"))
        cleanup_tree(self, outside)

        with self.assertRaises(BackendStartError):
            plan_for(self.root, request(self.root, working_directory=outside))

    def test_the_host_filesystem_mode_is_never_planned(self):
        with self.assertRaises(BackendStartError):
            plan_for(self.root, request(self.root, filesystem_mode="host"))

    def test_network_access_requires_a_configured_isolated_network(self):
        with self.assertRaises(BackendStartError):
            plan_for(self.root, request(self.root, network_access=True))

        plan = plan_for(
            self.root,
            request(self.root, network_access=True),
            config=ContainerLaunchConfig(
                image=image(),
                isolated_network="truecoder-isolated",
            ),
        )
        self.assertEqual(plan.security.network_mode, "isolated")

    def test_network_denial_is_exactly_none(self):
        plan = plan_for(self.root, request(self.root))

        self.assertEqual(plan.security.network_mode, "none")

    def test_effective_limits_are_clamped_to_the_configured_ceiling(self):
        plan = plan_for(
            self.root,
            request(self.root, memory_bytes=8 << 30, max_processes=10_000),
        )

        self.assertEqual(plan.security.memory_bytes, 512 * 1024 * 1024)
        self.assertEqual(plan.security.pids_limit, 64)

    def test_shell_mode_uses_the_pinned_container_shell(self):
        plan = plan_for(
            self.root,
            request(self.root, mode="shell", script="echo hi"),
        )

        self.assertEqual(plan.argv, ("/bin/sh", "-c", "echo hi"))

    def test_exec_mode_preserves_argv_exactly(self):
        plan = plan_for(
            self.root,
            request(self.root, argv=("python3", "-c", "print('x')")),
        )

        self.assertEqual(plan.argv, ("python3", "-c", "print('x')"))

    def test_labels_carry_the_exact_run_identity(self):
        plan = plan_for(self.root, request(self.root))

        labels = dict(plan.labels.as_pairs())
        self.assertEqual(labels[LABEL_MANAGED], "true")
        self.assertEqual(labels[LABEL_OWNERSHIP_TOKEN], TOKEN)
        self.assertEqual(plan.labels.audit_run_id, "run_plan_01")
        self.assertEqual(plan.labels.image_digest, DIGEST)

    def test_a_descriptor_change_after_preparation_is_refused(self):
        drifted = BackendDescriptor(
            name="container",
            available=True,
            capabilities=facts().capabilities(),
            version="30.0.0",
            runtime=runtime_info(),
        )

        with self.assertRaises(BackendStartError):
            build_container_plan(
                prepared(self.root, request(self.root)),
                start_context(self.root),
                drifted,
                ContainerLaunchConfig(image=image()),
                ownership_token=TOKEN,
            )

    @requires_posix_permissions
    def test_a_workspace_the_sandbox_user_cannot_read_is_refused(self):
        private = Path(tempfile.mkdtemp(prefix="tc-private-"))
        os.chmod(private, 0o700)
        cleanup_tree(self, private)

        with self.assertRaises(BackendStartError):
            build_container_plan(
                prepared(private, request(private)),
                start_context(private),
                descriptor(),
                ContainerLaunchConfig(image=image()),
                ownership_token=TOKEN,
            )


class EnvironmentFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = workspace()
        cleanup_tree(self, self.root)

    def test_values_are_written_as_key_value_lines(self):
        launch = PreparedExecution(
            request=request(self.root),
            backend=descriptor(),
            environment=construct_environment(
                platform="posix",
                inherited={"PATH": "/usr/bin"},
                requested=(("ADDED", "yes"),),
            ),
            resolved_shell=None,
        )

        content = build_env_file_content(launch)

        self.assertIn("ADDED=yes", content)
        self.assertIn("PATH=/usr/bin", content)

    def test_a_cpu_budget_is_passed_to_the_trusted_entrypoint(self):
        launch = prepared(self.root, request(self.root))

        content = build_env_file_content(launch, cpu_seconds=12.5)

        self.assertIn("TRUECODER_CPU_SECONDS=12.5", content)

    def test_newlines_in_values_are_rejected(self):
        launch = PreparedExecution(
            request=request(self.root),
            backend=descriptor(),
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(("BROKEN", "a\nb"),),
            ),
            resolved_shell=None,
        )

        with self.assertRaises(ValueError):
            build_env_file_content(launch)


class DockerArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = workspace()
        cleanup_tree(self, self.root)
        self.plan = plan_for(self.root, request(self.root))

    def test_every_required_hardening_flag_is_present(self):
        argv = docker_create_argv(self.plan)
        rendered = " ".join(argv)

        for expected in (
            "--pull never",
            "--restart no",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges=true",
            "--security-opt seccomp=builtin",
            "--network none",
            "--user 65532:65532",
            "--memory 536870912",
            "--memory-swap 536870912",
            "--pids-limit 64",
        ):
            self.assertIn(expected, rendered)

    def test_no_forbidden_flag_is_ever_rendered(self):
        argv = docker_create_argv(self.plan)

        for forbidden in FORBIDDEN_ARGUMENTS:
            self.assertNotIn(forbidden, argv)

    def test_the_image_and_command_end_the_argv(self):
        argv = docker_create_argv(self.plan)

        self.assertEqual(argv[-3], DIGEST)
        self.assertEqual(argv[-2:], ("python3", "-V"))

    def test_environment_values_never_appear_in_argv(self):
        env_file = self.root / "environment"
        plan = plan_for(self.root, request(self.root), env_file=env_file)

        argv = docker_create_argv(plan)

        self.assertIn("--env-file", argv)
        self.assertIn(str(env_file), argv)
        self.assertNotIn("PATH=/usr/bin", " ".join(argv))

    def test_tmpfs_targets_are_bounded_and_non_root(self):
        argv = docker_create_argv(self.plan)
        tmpfs = [argv[index + 1] for index, item in enumerate(argv) if item == "--tmpfs"]

        self.assertEqual(len(tmpfs), 3)
        for entry in tmpfs:
            self.assertIn("noexec", entry)
            self.assertIn("nosuid", entry)
            self.assertIn("nodev", entry)
            self.assertIn("uid=65532", entry)

    def test_stop_and_remove_require_a_full_identifier(self):
        self.assertEqual(
            docker_stop_argv(CONTAINER_ID, 2.0),
            ("stop", "--timeout", "2", CONTAINER_ID),
        )
        self.assertEqual(
            docker_remove_argv(CONTAINER_ID, force=True),
            ("rm", "--force", CONTAINER_ID),
        )
        with self.assertRaises(ValueError):
            docker_stop_argv("abc123", 1.0)


class InspectParsingTests(unittest.TestCase):
    def payload(self, **overrides) -> str:
        import json

        state = {"Status": "exited", "ExitCode": 3, "OOMKilled": False}
        state.update(overrides.pop("state", {}))
        entry = {
            "Id": CONTAINER_ID,
            "Image": DIGEST,
            "State": state,
            "Config": {"Labels": {LABEL_MANAGED: "true"}},
        }
        entry.update(overrides)
        return json.dumps([entry])

    def test_exact_fields_become_an_inspection(self):
        inspection = parse_docker_inspect(self.payload())

        self.assertEqual(inspection.container_id, CONTAINER_ID)
        self.assertEqual(inspection.state, "exited")
        self.assertEqual(inspection.exit_code, 3)
        self.assertFalse(inspection.oom_killed)
        self.assertTrue(inspection.terminal)

    def test_an_oom_kill_is_preserved(self):
        inspection = parse_docker_inspect(
            self.payload(state={"OOMKilled": True, "ExitCode": 137}),
        )

        self.assertTrue(inspection.oom_killed)

    def test_a_created_container_reports_no_exit_code(self):
        inspection = parse_docker_inspect(
            self.payload(state={"Status": "created", "ExitCode": 0}),
        )

        self.assertEqual(inspection.state, "created")
        self.assertIsNone(inspection.exit_code)

    def test_malformed_documents_are_rejected(self):
        for payload in (
            "[]",
            "[{}]",
            '[{"Id": "short"}]',
            '{"Id": "x"}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_docker_inspect(payload)

    def test_an_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_docker_inspect(self.payload(state={"Status": "teleporting"}))

    def test_oversized_payloads_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_docker_inspect("x" * (1024 * 1024 + 1))

    def test_container_ids_must_be_full_hex(self):
        self.assertEqual(parse_container_id(f"{CONTAINER_ID}\n"), CONTAINER_ID)
        with self.assertRaises(ValueError):
            parse_container_id("deadbeef")


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = workspace()
        cleanup_tree(self, self.root)
        self.plan = plan_for(self.root, request(self.root))
        self.resource = create_container_resource(
            self.plan,
            container_id=CONTAINER_ID,
            runtime_version="29.3.0",
            host_id="host-1",
        )

    def inspection(self, **overrides) -> ContainerInspection:
        labels = dict(self.plan.labels.as_pairs())
        labels.update(overrides.pop("labels", {}))
        values = {
            "container_id": CONTAINER_ID,
            "state": "created",
            "labels": tuple(sorted(labels.items())),
            "image_digest": DIGEST,
        }
        values.update(overrides)
        return ContainerInspection(**values)

    def test_the_resource_records_every_required_fact(self):
        facts_read = read_facts(self.resource)

        self.assertEqual(facts_read.container_id, CONTAINER_ID)
        self.assertEqual(facts_read.audit_run_id, "run_plan_01")
        self.assertEqual(facts_read.image_digest, DIGEST)
        self.assertEqual(self.resource.resource_id, "exec-plan-01")
        self.assertEqual(self.resource.ownership_token, TOKEN)

    def test_an_exact_match_reports_no_mismatch(self):
        self.assertEqual(
            verify_container_identity(
                self.resource,
                self.inspection(),
                host_id="host-1",
            ),
            (),
        )

    def test_every_single_field_mismatch_is_detected(self):
        cases = (
            ("host", {"host_id": "other-host"}),
            ("container-id", {"container_id": "d" * 64}),
            ("image-digest", {"image_digest": OTHER_DIGEST}),
            ("managed-label", {"labels": {LABEL_MANAGED: "false"}}),
            ("ownership-token-label", {"labels": {LABEL_OWNERSHIP_TOKEN: "z" * 64}}),
        )

        for expected, overrides in cases:
            with self.subTest(expected=expected):
                host_id = overrides.pop("host_id", "host-1")
                mismatches = verify_container_identity(
                    self.resource,
                    self.inspection(**overrides),
                    host_id=host_id,
                )
                self.assertIn(expected, mismatches)


class CapabilityTruthTests(unittest.TestCase):
    def test_a_verified_runtime_is_available(self):
        self.assertTrue(facts().available)
        self.assertEqual(facts().unavailable_reasons(), ())

    def test_a_missing_image_makes_the_sandbox_unavailable(self):
        reasons = facts(image=None).unavailable_reasons()

        self.assertIn("sandbox-image-missing", tuple(r.code for r in reasons))

    def test_an_unimplemented_dialect_makes_the_sandbox_unavailable(self):
        reasons = facts(dialect_implemented=False).unavailable_reasons()

        self.assertIn(
            "runtime-dialect-not-implemented",
            tuple(r.code for r in reasons),
        )

    def test_missing_security_options_make_the_sandbox_unavailable(self):
        reasons = facts(supports_none_network=False).unavailable_reasons()

        self.assertIn(
            "container-security-option-unsupported",
            tuple(r.code for r in reasons),
        )

    def test_cpu_enforcement_is_advertised_honestly(self):
        self.assertEqual(facts().capabilities().cpu_limits, "best_effort")
        self.assertEqual(
            facts(cpu_enforcement="enforced").capabilities().cpu_limits,
            "enforced",
        )


class SecurityModelTests(unittest.TestCase):
    def test_forbidden_mount_sources_are_rejected(self):
        for source in ("/var/run/docker.sock", "/etc", "/dev", "/root"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                ContainerMount(
                    source=Path(source),
                    target=CONTAINER_WORKSPACE,
                    read_only=True,
                )

    def test_weakened_hardening_is_rejected(self):
        base = {
            "memory_bytes": 512 * 1024 * 1024,
            "pids_limit": 64,
        }
        for override in (
            {"read_only_root": False},
            {"drop_all_capabilities": False},
            {"no_new_privileges": False},
            {"seccomp_profile": "unconfined"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                ContainerSecurityProfile(**base, **override)

    def test_unapproved_tmpfs_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            ContainerTmpfs(
                target=PurePosixPath("/workspace"),
                size_bytes=1024,
                uid=65532,
                gid=65532,
            )

    def test_a_root_image_user_is_rejected(self):
        with self.assertRaises(ValueError):
            ContainerImage(
                reference=DIGEST,
                digest=DIGEST,
                platform="linux/amd64",
                user="0:0",
            )

    def test_an_unpinned_image_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            ContainerImage(
                reference="truecoder-exec:latest",
                digest=DIGEST,
                platform="linux/amd64",
                user="65532:65532",
            )

    def test_labels_must_record_the_planned_image_digest(self):
        with self.assertRaises(ValueError):
            ContainerLabels(
                execution_id="exec",
                audit_run_id="run",
                ownership_token=TOKEN,
                image_digest="not-a-digest",
            )


if __name__ == "__main__":
    unittest.main()
