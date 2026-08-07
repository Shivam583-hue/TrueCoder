from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from truecoder.execution.configuration import (
    EXECUTION_CONFIG_VERSION,
    ExecutionConfigError,
    load_execution_config,
    parse_execution_config,
)
from truecoder.execution.models import RiskLevel


class ExecutionConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def parse(self, payload: dict):
        return parse_execution_config(
            json.dumps(payload),
            base_directory=self.root,
        )

    def test_a_missing_file_uses_zero_configuration_defaults(self):
        config = load_execution_config(self.root / "missing.json")

        self.assertTrue(config.enabled)
        self.assertEqual(config.policy_config.limit_ceiling.timeout_seconds, 600)
        self.assertIsNone(config.container_isolated_network)

    def test_every_advanced_section_maps_into_bootstrap_configuration(self):
        config = self.parse(
            {
                "version": EXECUTION_CONFIG_VERSION,
                "enabled": False,
                "audit_database_path": "state/audit.sqlite3",
                "image_lock_path": "container/image.lock",
                "trusted_rules_path": "trusted.json",
                "limits": {
                    "timeout_seconds": 30,
                    "max_output_bytes": 8192,
                    "max_return_bytes": 4096,
                    "memory_bytes": 1048576,
                    "cpu_seconds": 5,
                    "max_processes": 8,
                    "termination_grace_seconds": 1,
                },
                "policy": {
                    "version": "custom-v1",
                    "minimum_isolation": "best_effort",
                    "limit_enforcement": "best_effort",
                    "unknown_risk": "high",
                },
                "environment": {
                    "additional_inherited_names": ["CI"],
                    "include_home_paths": True,
                    "max_inherited_entries": 64,
                },
                "container": {
                    "default_memory_bytes": 1048576,
                    "default_pids_limit": 16,
                    "cpu_rate_ceiling": 1.5,
                    "isolated_network": "truecoder-isolated",
                },
                "retention": {
                    "days": 45,
                },
            }
        )

        self.assertFalse(config.enabled)
        self.assertEqual(
            config.audit_database_path,
            self.root / "state" / "audit.sqlite3",
        )
        self.assertEqual(config.policy_config.version, "custom-v1")
        self.assertIs(config.policy_config.unknown_risk, RiskLevel.HIGH)
        self.assertEqual(config.policy_config.limit_ceiling.cpu_seconds, 5)
        self.assertEqual(
            config.environment_policy.additional_inherited_names,
            ("CI",),
        )
        self.assertTrue(config.environment_policy.include_home_paths)
        self.assertEqual(config.container_default_memory_bytes, 1048576)
        self.assertEqual(config.container_default_pids_limit, 16)
        self.assertEqual(config.container_cpu_rate_ceiling, 1.5)
        self.assertEqual(config.container_isolated_network, "truecoder-isolated")
        self.assertEqual(config.retention_policy.days, 45)

    def test_unknown_fields_are_refused(self):
        with self.assertRaises(ExecutionConfigError):
            self.parse({"version": 1, "surprise": True})

    def test_unknown_versions_are_refused(self):
        with self.assertRaises(ExecutionConfigError):
            self.parse({"version": EXECUTION_CONFIG_VERSION + 1})

    def test_invalid_limits_are_reported_as_configuration_errors(self):
        with self.assertRaises(ExecutionConfigError):
            self.parse(
                {
                    "version": 1,
                    "limits": {
                        "timeout_seconds": -1,
                    },
                }
            )

    def test_invalid_environment_names_are_reported_cleanly(self):
        with self.assertRaises(ExecutionConfigError):
            self.parse(
                {
                    "version": 1,
                    "environment": {
                        "additional_inherited_names": [""],
                    },
                }
            )

    def test_relative_paths_are_resolved_from_the_configuration_directory(self):
        config = self.parse(
            {
                "version": 1,
                "image_lock_path": "../image.lock",
            }
        )

        self.assertEqual(
            config.image_lock_path,
            (self.root / ".." / "image.lock").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
