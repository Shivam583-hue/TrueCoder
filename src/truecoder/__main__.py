import asyncio

from truecoder.client.llm_client import LLMClient


async def main():
    client = LLMClient()
    messages = [{"role": "user", "content": "What's up"}]
    await client.chat_completion(messages, False)
    print("done")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
