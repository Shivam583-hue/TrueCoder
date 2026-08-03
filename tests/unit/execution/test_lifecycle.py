from __future__ import annotations

import asyncio
import unittest
from itertools import combinations, pairwise, permutations

from truecoder.execution.errors import InvalidExecutionStateError
from truecoder.execution.lifecycle import (
    TERMINAL_PRIORITY,
    ClaimOutcome,
    LifecycleState,
    RunState,
    TerminalArbiter,
    TerminalClaim,
    TerminalSettlement,
    resolve_terminal_claim,
)
from truecoder.execution.models import ExecutionStatus, TerminationReason

STATUS_BY_SOURCE = {
    "backend_exit": ("completed", None),
    "output_limit": ("limit_exceeded", "output_limit"),
    "resource_limit": ("limit_exceeded", "memory_limit"),
    "cancellation": ("cancelled", "cancellation"),
    "timeout": ("timed_out", "timeout"),
}

LEGAL_TRANSITIONS = {
    RunState.ADMITTED: frozenset({RunState.POLICY_EVALUATED}),
    RunState.POLICY_EVALUATED: frozenset(
        {RunState.PREPARED, RunState.FINALIZING},
    ),
    RunState.PREPARED: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.REGISTERED},
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.REGISTERED, RunState.FINALIZING},
    ),
    RunState.REGISTERED: frozenset({RunState.STARTING, RunState.FINALIZING}),
    RunState.STARTING: frozenset({RunState.RUNNING, RunState.FINALIZING}),
    RunState.RUNNING: frozenset({RunState.TERMINATING, RunState.FINALIZING}),
    RunState.TERMINATING: frozenset({RunState.FINALIZING}),
    RunState.FINALIZING: frozenset({RunState.TERMINAL}),
    RunState.TERMINAL: frozenset(),
}

LINEAR_PATH = (
    RunState.POLICY_EVALUATED,
    RunState.PREPARED,
    RunState.AWAITING_APPROVAL,
    RunState.REGISTERED,
    RunState.STARTING,
    RunState.RUNNING,
    RunState.TERMINATING,
    RunState.FINALIZING,
    RunState.TERMINAL,
)


def claim(source: str, observed_at: float = 1.0) -> TerminalClaim:
    status, reason = STATUS_BY_SOURCE[source]
    return TerminalClaim(
        status=status,
        reason=reason,
        observed_at_monotonic=observed_at,
        source=source,
    )


def path_to(state: RunState) -> tuple[RunState, ...]:
    if state is RunState.ADMITTED:
        return ()
    return LINEAR_PATH[: LINEAR_PATH.index(state) + 1]


def machine_in(state: RunState) -> LifecycleState:
    machine = LifecycleState("exec_01")
    for step in path_to(state):
        machine.transition(step)
    return machine


class TerminalClaimTests(unittest.TestCase):
    def test_preserves_the_observed_terminal_facts(self):
        observed = claim("timeout", observed_at=12.5)

        self.assertEqual(observed.status, "timed_out")
        self.assertEqual(observed.reason, "timeout")
        self.assertEqual(observed.observed_at_monotonic, 12.5)
        self.assertEqual(observed.source, "timeout")
        self.assertEqual(observed, claim("timeout", observed_at=12.5))

    def test_rejects_invalid_field_types_and_values(self):
        valid = {
            "status": "completed",
            "reason": None,
            "observed_at_monotonic": 1.0,
            "source": "backend_exit",
        }
        invalid_arguments = (
            {"status": 1},
            {"status": ""},
            {"status": "unknown"},
            {"status": "timed_out", "reason": "unknown"},
            {"status": "timed_out", "reason": 1},
            {"observed_at_monotonic": "1.0"},
            {"observed_at_monotonic": True},
            {"observed_at_monotonic": float("nan")},
            {"observed_at_monotonic": float("inf")},
            {"source": 1},
            {"source": "   "},
            {"source": "backend exit"},
        )

        for overrides in invalid_arguments:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                TerminalClaim(**{**valid, **overrides})  # type: ignore[arg-type]

    def test_rejects_a_status_that_contradicts_its_termination_reason(self):
        incoherent: tuple[tuple[ExecutionStatus, TerminationReason | None], ...] = (
            ("completed", "timeout"),
            ("failed", "cancellation"),
            ("denied", "shutdown"),
            ("failed_to_start", "timeout"),
            ("timed_out", None),
            ("timed_out", "cancellation"),
            ("cancelled", None),
            ("cancelled", "output_limit"),
            ("limit_exceeded", None),
            ("limit_exceeded", "timeout"),
        )

        for status, reason in incoherent:
            with (
                self.subTest(status=status, reason=reason),
                self.assertRaises(ValueError),
            ):
                TerminalClaim(
                    status=status,
                    reason=reason,
                    observed_at_monotonic=1.0,
                    source="backend_exit",
                )

    def test_accepts_every_coherent_status_and_reason_pair(self):
        coherent: tuple[tuple[ExecutionStatus, TerminationReason | None], ...] = (
            ("completed", None),
            ("failed", None),
            ("denied", None),
            ("failed_to_start", None),
            ("timed_out", "timeout"),
            ("cancelled", "cancellation"),
            ("cancelled", "shutdown"),
            ("limit_exceeded", "output_limit"),
            ("limit_exceeded", "memory_limit"),
            ("limit_exceeded", "cpu_limit"),
            ("limit_exceeded", "process_limit"),
        )

        for status, reason in coherent:
            with self.subTest(status=status, reason=reason):
                observed = TerminalClaim(
                    status=status,
                    reason=reason,
                    observed_at_monotonic=0.0,
                    source="backend_exit",
                )
                self.assertEqual(observed.status, status)
                self.assertEqual(observed.reason, reason)


class ClaimOutcomeTests(unittest.TestCase):
    def test_carries_both_the_verdict_and_the_effective_claim(self):
        winner = claim("backend_exit")
        outcome = ClaimOutcome(won=False, claim=winner)

        self.assertFalse(outcome.won)
        self.assertIs(outcome.claim, winner)

    def test_rejects_invalid_arguments(self):
        with self.assertRaises(TypeError):
            ClaimOutcome(won="yes", claim=claim("timeout"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ClaimOutcome(won=True, claim="timeout")  # type: ignore[arg-type]


class TerminalSettlementTests(unittest.TestCase):
    def test_records_a_durable_finalization(self):
        winner = claim("backend_exit")
        settlement = TerminalSettlement.recorded(winner)

        self.assertIs(settlement.claim, winner)
        self.assertTrue(settlement.finalized)
        self.assertIsNone(settlement.failure)

    def test_separates_a_won_claim_from_a_failed_finalization(self):
        winner = claim("timeout")
        settlement = TerminalSettlement.unrecorded(winner, "audit store is locked")

        self.assertIs(settlement.claim, winner)
        self.assertFalse(settlement.finalized)
        self.assertEqual(settlement.failure, "audit store is locked")

    def test_rejects_settlements_that_misreport_durability(self):
        winner = claim("cancellation")
        invalid_arguments = (
            {"claim": "not a claim", "finalized": True},
            {"claim": winner, "finalized": "yes"},
            {"claim": winner, "finalized": True, "failure": "unwritten"},
            {"claim": winner, "finalized": False},
            {"claim": winner, "finalized": False, "failure": "  "},
            {"claim": winner, "finalized": False, "failure": 1},
        )

        for arguments in invalid_arguments:
            with (
                self.subTest(arguments=arguments),
                self.assertRaises((TypeError, ValueError)),
            ):
                TerminalSettlement(**arguments)  # type: ignore[arg-type]


class TerminalPriorityTests(unittest.TestCase):
    def test_priority_order_is_the_documented_one(self):
        self.assertEqual(
            TERMINAL_PRIORITY,
            (
                "backend_exit",
                "output_limit",
                "resource_limit",
                "cancellation",
                "timeout",
            ),
        )
        self.assertEqual(sorted(TERMINAL_PRIORITY), sorted(STATUS_BY_SOURCE))

    def test_each_adjacent_priority_pair_resolves_in_order(self):
        for higher, lower in pairwise(TERMINAL_PRIORITY):
            with self.subTest(higher=higher, lower=lower):
                # The loser is observed first, so only priority can decide.
                winner = claim(higher, observed_at=9.0)
                loser = claim(lower, observed_at=1.0)

                self.assertIs(resolve_terminal_claim((winner, loser)), winner)
                self.assertIs(resolve_terminal_claim((loser, winner)), winner)

    def test_every_priority_pair_resolves_in_order(self):
        for higher, lower in combinations(TERMINAL_PRIORITY, 2):
            with self.subTest(higher=higher, lower=lower):
                winner = claim(higher, observed_at=9.0)
                loser = claim(lower, observed_at=1.0)

                self.assertIs(resolve_terminal_claim((winner, loser)), winner)
                self.assertIs(resolve_terminal_claim((loser, winner)), winner)

    def test_full_same_tick_race_always_selects_the_backend_exit(self):
        candidates = tuple(
            claim(source, observed_at=5.0) for source in TERMINAL_PRIORITY
        )
        winner = candidates[0]

        for ordering in permutations(candidates):
            with self.subTest(ordering=tuple(item.source for item in ordering)):
                self.assertIs(resolve_terminal_claim(ordering), winner)

    def test_an_enforced_limit_beats_a_generic_timeout(self):
        deadline = claim("timeout", observed_at=1.0)

        for source in ("output_limit", "resource_limit"):
            with self.subTest(source=source):
                enforced = claim(source, observed_at=1.0)
                self.assertIs(
                    resolve_terminal_claim((deadline, enforced)),
                    enforced,
                )

    def test_priority_outranks_observation_time(self):
        early_timeout = claim("timeout", observed_at=0.0)
        late_exit = claim("backend_exit", observed_at=1000.0)

        self.assertIs(
            resolve_terminal_claim((early_timeout, late_exit)),
            late_exit,
        )

    def test_same_source_ties_prefer_the_earliest_observation(self):
        for source in TERMINAL_PRIORITY:
            with self.subTest(source=source):
                first = claim(source, observed_at=1.0)
                second = claim(source, observed_at=1.5)

                self.assertIs(resolve_terminal_claim((first, second)), first)
                self.assertIs(resolve_terminal_claim((second, first)), first)

    def test_identical_claims_resolve_to_the_first_candidate(self):
        first = claim("cancellation", observed_at=2.0)
        second = claim("cancellation", observed_at=2.0)

        self.assertEqual(first, second)
        self.assertIs(resolve_terminal_claim((first, second)), first)
        self.assertIs(resolve_terminal_claim((second, first)), second)

    def test_a_single_candidate_wins_unchanged(self):
        only = claim("output_limit")

        self.assertIs(resolve_terminal_claim((only,)), only)

    def test_rejects_unknown_sources_loudly(self):
        unknown = TerminalClaim(
            status="failed",
            reason=None,
            observed_at_monotonic=1.0,
            source="mystery",
        )

        with self.assertRaisesRegex(ValueError, "mystery"):
            resolve_terminal_claim((unknown,))

        with self.assertRaisesRegex(ValueError, "mystery"):
            resolve_terminal_claim((claim("timeout"), unknown))

    def test_rejects_malformed_candidate_collections(self):
        with self.assertRaises(TypeError):
            resolve_terminal_claim([claim("timeout")])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            resolve_terminal_claim(())
        with self.assertRaises(TypeError):
            resolve_terminal_claim(("timeout",))  # type: ignore[arg-type]


class LifecycleStateTests(unittest.TestCase):
    def test_starts_admitted_with_a_normalized_execution_id(self):
        machine = LifecycleState(" exec_01 ")

        self.assertEqual(machine.execution_id, "exec_01")
        self.assertIs(machine.current, RunState.ADMITTED)
        self.assertEqual(machine.history, (RunState.ADMITTED,))
        self.assertFalse(machine.is_terminal)

    def test_rejects_invalid_execution_ids(self):
        with self.assertRaises(TypeError):
            LifecycleState(1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            LifecycleState("   ")

    def test_walks_the_complete_linear_path(self):
        machine = LifecycleState("exec_01")

        for state in LINEAR_PATH:
            self.assertIs(machine.transition(state), state)

        self.assertIs(machine.current, RunState.TERMINAL)
        self.assertTrue(machine.is_terminal)
        self.assertEqual(machine.history, (RunState.ADMITTED, *LINEAR_PATH))

    def test_every_transition_matches_the_documented_table(self):
        for source in RunState:
            for target in RunState:
                legal = target in LEGAL_TRANSITIONS[source]
                with self.subTest(source=source, target=target, legal=legal):
                    machine = machine_in(source)
                    self.assertIs(machine.current, source)
                    self.assertIs(machine.can_transition(target), legal)

                    if legal:
                        self.assertIs(machine.transition(target), target)
                        self.assertIs(machine.current, target)
                        continue

                    with self.assertRaises(InvalidExecutionStateError):
                        machine.transition(target)
                    self.assertIs(machine.current, source)

    def test_skips_approval_when_none_is_required(self):
        machine = LifecycleState("exec_01")
        machine.transition(RunState.POLICY_EVALUATED)
        machine.transition(RunState.PREPARED)
        machine.transition(RunState.REGISTERED)

        self.assertIs(machine.current, RunState.REGISTERED)
        self.assertNotIn(RunState.AWAITING_APPROVAL, machine.history)

    def test_policy_denial_finalizes_before_any_backend_exists(self):
        machine = LifecycleState("exec_01")
        machine.transition(RunState.POLICY_EVALUATED)
        machine.transition(RunState.FINALIZING)
        machine.transition(RunState.TERMINAL)

        self.assertEqual(
            machine.history,
            (
                RunState.ADMITTED,
                RunState.POLICY_EVALUATED,
                RunState.FINALIZING,
                RunState.TERMINAL,
            ),
        )

    def test_approval_rejection_finalizes_before_any_backend_exists(self):
        machine = machine_in(RunState.AWAITING_APPROVAL)
        machine.transition(RunState.FINALIZING)
        machine.transition(RunState.TERMINAL)

        self.assertEqual(
            machine.history,
            (
                RunState.ADMITTED,
                RunState.POLICY_EVALUATED,
                RunState.PREPARED,
                RunState.AWAITING_APPROVAL,
                RunState.FINALIZING,
                RunState.TERMINAL,
            ),
        )

    def test_a_failed_start_finalizes_without_terminating(self):
        machine = machine_in(RunState.STARTING)
        machine.transition(RunState.FINALIZING)

        self.assertIs(machine.current, RunState.FINALIZING)
        self.assertNotIn(RunState.RUNNING, machine.history)

    def test_a_natural_exit_finalizes_without_terminating(self):
        machine = machine_in(RunState.RUNNING)
        machine.transition(RunState.FINALIZING)

        self.assertIs(machine.current, RunState.FINALIZING)
        self.assertNotIn(RunState.TERMINATING, machine.history)

    def test_representative_illegal_moves_raise_with_context(self):
        illegal = (
            (RunState.ADMITTED, RunState.RUNNING),
            (RunState.ADMITTED, RunState.FINALIZING),
            (RunState.POLICY_EVALUATED, RunState.AWAITING_APPROVAL),
            (RunState.POLICY_EVALUATED, RunState.STARTING),
            (RunState.PREPARED, RunState.FINALIZING),
            (RunState.AWAITING_APPROVAL, RunState.PREPARED),
            (RunState.REGISTERED, RunState.RUNNING),
            (RunState.STARTING, RunState.TERMINATING),
            (RunState.RUNNING, RunState.TERMINAL),
            (RunState.TERMINATING, RunState.RUNNING),
            (RunState.FINALIZING, RunState.RUNNING),
            (RunState.TERMINAL, RunState.FINALIZING),
        )

        for source, target in illegal:
            with self.subTest(source=source, target=target):
                machine = machine_in(source)

                with self.assertRaises(InvalidExecutionStateError) as raised:
                    machine.transition(target)

                self.assertEqual(raised.exception.execution_id, "exec_01")
                self.assertEqual(
                    raised.exception.operation,
                    "lifecycle_transition",
                )
                self.assertIn(source.value, str(raised.exception))
                self.assertIn(target.value, str(raised.exception))
                self.assertEqual(machine.history, (RunState.ADMITTED, *path_to(source)))

    def test_terminal_is_a_dead_end(self):
        machine = machine_in(RunState.TERMINAL)

        self.assertTrue(machine.is_terminal)
        for state in RunState:
            with self.subTest(state=state):
                self.assertFalse(machine.can_transition(state))

    def test_rejects_targets_that_are_not_run_states(self):
        machine = LifecycleState("exec_01")

        with self.assertRaises(TypeError):
            machine.can_transition("running")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            machine.transition("running")  # type: ignore[arg-type]


class TerminalArbiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_has_no_winner_before_the_first_claim(self):
        arbiter = TerminalArbiter()

        self.assertIsNone(await arbiter.winner())

    async def test_the_first_claim_owns_the_terminal_path(self):
        arbiter = TerminalArbiter()
        first = claim("cancellation", observed_at=1.0)
        second = claim("backend_exit", observed_at=1.1)

        won = await arbiter.claim(first)
        lost = await arbiter.claim(second)

        self.assertTrue(won.won)
        self.assertIs(won.claim, first)
        self.assertFalse(lost.won)
        self.assertIs(lost.claim, first)
        self.assertIs(await arbiter.winner(), first)

    async def test_repeating_the_winning_claim_changes_nothing(self):
        arbiter = TerminalArbiter()
        winner = claim("timeout")

        first = await arbiter.claim(winner)
        second = await arbiter.claim(winner)

        self.assertTrue(first.won)
        self.assertFalse(second.won)
        self.assertIs(second.claim, winner)
        self.assertIs(await arbiter.winner(), winner)

    async def test_a_higher_priority_claim_cannot_replace_a_seated_winner(self):
        arbiter = TerminalArbiter()
        seated = claim("timeout")

        await arbiter.claim(seated)
        outcome = await arbiter.claim(claim("backend_exit"))

        self.assertFalse(outcome.won)
        self.assertIs(outcome.claim, seated)
        self.assertIs(await arbiter.winner(), seated)

    async def test_concurrent_claims_produce_exactly_one_winner(self):
        arbiter = TerminalArbiter()
        release = asyncio.Event()
        candidates = tuple(
            claim(source, observed_at=float(index))
            for index, source in enumerate(TERMINAL_PRIORITY * 4)
        )

        async def contend(candidate: TerminalClaim) -> ClaimOutcome:
            await release.wait()
            return await arbiter.claim(candidate)

        tasks = [asyncio.create_task(contend(candidate)) for candidate in candidates]
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(*tasks)

        winner = await arbiter.winner()
        self.assertEqual(sum(outcome.won for outcome in outcomes), 1)
        self.assertTrue(all(outcome.claim is winner for outcome in outcomes))
        self.assertIn(winner, candidates)

    async def test_a_resolved_same_tick_race_is_claimed_as_one_winner(self):
        arbiter = TerminalArbiter()
        candidates = tuple(
            claim(source, observed_at=3.0) for source in TERMINAL_PRIORITY
        )

        outcome = await arbiter.claim(resolve_terminal_claim(candidates))

        self.assertTrue(outcome.won)
        self.assertEqual(outcome.claim.source, "backend_exit")
        self.assertIs(await arbiter.winner(), candidates[0])

    async def test_rejects_candidates_that_are_not_terminal_claims(self):
        arbiter = TerminalArbiter()

        with self.assertRaises(TypeError):
            await arbiter.claim("timeout")  # type: ignore[arg-type]
        self.assertIsNone(await arbiter.winner())


if __name__ == "__main__":
    unittest.main()
