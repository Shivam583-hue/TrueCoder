import unittest

from truecoder.agent.messages import (
    copy_message,
    copy_messages,
    create_assistant_text_message,
    create_assistant_tool_call,
    create_assistant_tool_call_message,
    create_system_message,
    create_tool_message,
    create_user_message,
)
from truecoder.tools import ToolCall


class MessageConstructorTests(unittest.TestCase):
    def test_system_message_shape(self):
        self.assertEqual(
            create_system_message("You are helpful."),
            {"role": "system", "content": "You are helpful."},
        )

    def test_user_message_shape(self):
        self.assertEqual(
            create_user_message("Read the file."),
            {"role": "user", "content": "Read the file."},
        )

    def test_assistant_text_message_shape(self):
        self.assertEqual(
            create_assistant_text_message("Here it is."),
            {"role": "assistant", "content": "Here it is."},
        )

    def test_assistant_tool_call_shape(self):
        call = ToolCall(
            call_id="call_1",
            name="read_file",
            arguments_json='{"path": "main.py"}',
        )

        self.assertEqual(
            create_assistant_tool_call(call),
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "main.py"}',
                },
            },
        )

    def test_assistant_tool_call_message_defaults_to_null_content(self):
        call = ToolCall(
            call_id="call_1",
            name="read_file",
            arguments_json='{"path": "main.py"}',
        )

        self.assertEqual(
            create_assistant_tool_call_message([call]),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "main.py"}',
                        },
                    }
                ],
            },
        )

    def test_assistant_tool_call_message_preserves_content_and_order(self):
        first = ToolCall(call_id="call_1", name="read_file", arguments_json="{}")
        second = ToolCall(call_id="call_2", name="list_dir", arguments_json="{}")

        message = create_assistant_tool_call_message(
            [first, second],
            content="Working on it.",
        )

        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "Working on it.")
        self.assertEqual(
            [call["id"] for call in message["tool_calls"]],
            ["call_1", "call_2"],
        )

    def test_tool_message_shape(self):
        self.assertEqual(
            create_tool_message("call_1", '{"content": "print(1)"}'),
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"content": "print(1)"}',
            },
        )

    def test_raw_argument_json_is_left_unchanged(self):
        raw_arguments = '{"path":"main.py",   "start": 1,\n"end":10}'
        call = ToolCall(
            call_id="call_1",
            name="read_file",
            arguments_json=raw_arguments,
        )

        message = create_assistant_tool_call_message([call])

        self.assertEqual(
            message["tool_calls"][0]["function"]["arguments"],
            raw_arguments,
        )


class MessageCopyTests(unittest.TestCase):
    def test_copy_message_is_a_deep_copy(self):
        call = ToolCall(call_id="call_1", name="read_file", arguments_json="{}")
        original = create_assistant_tool_call_message([call])

        duplicate = copy_message(original)
        # Mutating the source's nested arguments must not reach the copy.
        original["tool_calls"][0]["function"]["arguments"] = "hacked"

        self.assertEqual(
            duplicate,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
        )

    def test_copy_messages_protects_nested_function_arguments(self):
        call = ToolCall(call_id="call_1", name="read_file", arguments_json="{}")
        tool_call_message = create_assistant_tool_call_message([call])
        originals = [create_user_message("Read it"), tool_call_message]

        duplicates = copy_messages(originals)
        duplicates.append(create_user_message("injected"))
        # Mutating the source after copying must not reach the copies.
        tool_call_message["tool_calls"][0]["function"]["name"] = "deleted"

        self.assertEqual(duplicates[0], {"role": "user", "content": "Read it"})
        self.assertEqual(
            duplicates[1],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
        )
        self.assertEqual(len(originals), 2)


if __name__ == "__main__":
    unittest.main()
