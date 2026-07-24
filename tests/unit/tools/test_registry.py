import unittest

from truecoder.tools import (
    BaseTool,
    DuplicateToolError,
    ToolApproval,
    ToolArguments,
    ToolNotFoundError,
    ToolRegistry,
)


class ValueArguments(ToolArguments):
    value: int


class FirstTool(BaseTool[ValueArguments]):
    name = "first"
    description = "Return the supplied value."
    arguments_type = ValueArguments
    approval = ToolApproval.NOT_REQUIRED

    async def run(self, arguments: ValueArguments) -> int:
        return arguments.value


class SecondTool(FirstTool):
    name = "second"
    description = "Return the second supplied value."


class InvalidNameTool(FirstTool):
    name = "invalid name"


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.first = FirstTool()
        self.second = SecondTool()

    def test_registers_and_looks_up_tools_in_registration_order(self):
        self.registry.register(self.first)
        self.registry.register(self.second)

        self.assertIs(self.registry.get("first"), self.first)
        self.assertIs(self.registry.get("second"), self.second)
        self.assertEqual(self.registry.all(), (self.first, self.second))
        self.assertIn("first", self.registry)
        self.assertNotIn("missing", self.registry)

    def test_rejects_duplicate_names(self):
        self.registry.register(self.first)

        with self.assertRaisesRegex(DuplicateToolError, "first"):
            self.registry.register(FirstTool())

    def test_unknown_name_raises_domain_error(self):
        with self.assertRaisesRegex(ToolNotFoundError, "missing"):
            self.registry.get("missing")

    def test_definitions_are_ordered_and_return_fresh_schemas(self):
        self.registry.register(self.first)
        self.registry.register(self.second)

        definitions = self.registry.definitions()
        definitions[0]["function"]["parameters"]["type"] = "array"

        fresh_definitions = self.registry.definitions()
        self.assertEqual(
            [definition["function"]["name"] for definition in fresh_definitions],
            ["first", "second"],
        )
        self.assertEqual(
            fresh_definitions[0]["function"]["parameters"]["type"],
            "object",
        )

    def test_registration_validates_type_and_metadata_immediately(self):
        with self.assertRaises(TypeError):
            self.registry.register(object())  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            self.registry.register(InvalidNameTool())


if __name__ == "__main__":
    unittest.main()
