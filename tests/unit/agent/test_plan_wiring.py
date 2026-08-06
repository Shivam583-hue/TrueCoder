import json
import unittest

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, AgentEventType, ContextBuilder
from truecoder.agent.prompts import PLAN_TOOL_GUIDANCE
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.planning import PlanStore
from truecoder.tools import ToolCall, ToolRegistry
from truecoder.tools.builtin import UpdatePlanTool

PLAN_ARGUMENTS = json.dumps(
    {
        "steps": [
            {"title": "Read the failing test", "status": "done"},
            {"title": "Fix the parser", "status": "in_progress"},
        ]
    }
)


def _builder(plan_store: PlanStore | None = None) -> ContextBuilder:
    return ContextBuilder(
        system_prompt="test system",
        max_input_tokens=100,
        token_counter=FixedTokenCounter(),
        plan_store=plan_store,
    )


def _agent(
    client,
    plan_store: PlanStore | None = None,
    tool_registry: ToolRegistry | None = None,
) -> Agent:
    return Agent(
        llm_client=client,
        context_builder=_builder(),
        tool_registry=tool_registry,
        plan_store=plan_store,
    )


class PlanWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_no_plan_tool_without_a_store(self):
        agent = _agent(ScriptedLLMClient([]))

        self.assertNotIn("update_plan", agent.tool_registry)
        self.assertIsNone(agent.plan_store)
        self.assertIsNone(agent.context_builder.plan_store)
        self.assertNotIn(PLAN_TOOL_GUIDANCE.strip(), agent.context_builder.system_prompt)

    def test_a_store_registers_the_tool_and_enables_guidance(self):
        store = PlanStore()

        agent = _agent(ScriptedLLMClient([]), plan_store=store)

        self.assertIn("update_plan", agent.tool_registry)
        self.assertIsInstance(agent.tool_registry.get("update_plan"), UpdatePlanTool)
        self.assertIs(agent.plan_store, store)
        self.assertIs(agent.context_builder.plan_store, store)
        self.assertIn(PLAN_TOOL_GUIDANCE.strip(), agent.context_builder.system_prompt)

    def test_the_registered_tool_shares_the_agent_store(self):
        store = PlanStore()

        agent = _agent(ScriptedLLMClient([]), plan_store=store)

        tool = agent.tool_registry.get("update_plan")
        assert isinstance(tool, UpdatePlanTool)
        self.assertIs(tool.store, store)

    def test_an_existing_plan_tool_is_left_alone(self):
        store = PlanStore()
        preregistered = UpdatePlanTool(PlanStore())
        registry = ToolRegistry()
        registry.register(preregistered)

        agent = _agent(
            ScriptedLLMClient([]),
            plan_store=store,
            tool_registry=registry,
        )

        self.assertIs(agent.tool_registry.get("update_plan"), preregistered)

    def test_a_non_store_is_rejected(self):
        with self.assertRaises(TypeError):
            _agent(ScriptedLLMClient([]), plan_store=object())  # type: ignore[arg-type]

    async def test_the_plan_tool_runs_without_approval_and_reaches_the_next_request(
        self,
    ):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(
                            ToolCall("call_1", "update_plan", PLAN_ARGUMENTS),
                        ),
                    )
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Done"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE),
                ],
            ]
        )
        agent = Agent(
            llm_client=client,
            context_builder=_builder(),
            plan_store=store,
        )

        events = [event async for event in agent.run("fix the parser")]

        plan = store.current
        assert plan is not None
        self.assertEqual(
            [step.title for step in plan.steps],
            ["Read the failing test", "Fix the parser"],
        )
        self.assertNotIn(
            AgentEventType.TOOL_REJECTED,
            [event.type for event in events],
        )
        self.assertNotIn(
            AgentEventType.APPROVAL_REQUESTED,
            [event.type for event in events],
        )

        second_request = client.calls[1]["messages"]
        self.assertEqual(second_request[-1]["role"], "system")
        self.assertEqual(second_request[-1]["content"], plan.render())

    async def test_the_first_request_carries_no_plan(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Done"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE),
                ]
            ]
        )
        agent = Agent(
            llm_client=client,
            context_builder=_builder(),
            plan_store=store,
        )

        [event async for event in agent.run("say hello")]

        messages = client.calls[0]["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])

    async def test_an_invalid_plan_is_reported_back_to_the_model(self):
        store = PlanStore()
        arguments = json.dumps(
            {
                "steps": [
                    {"title": "A", "status": "in_progress"},
                    {"title": "B", "status": "in_progress"},
                ]
            }
        )
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "update_plan", arguments),),
                    )
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Sorry"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE),
                ],
            ]
        )
        agent = Agent(
            llm_client=client,
            context_builder=_builder(),
            plan_store=store,
        )

        events = [event async for event in agent.run("fix the parser")]

        self.assertIsNone(store.current)
        results = [
            event for event in events if event.type is AgentEventType.TOOL_RESULT
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].data["status"], "error")
        self.assertIn("invalid_plan", results[0].data["content"])


if __name__ == "__main__":
    unittest.main()
