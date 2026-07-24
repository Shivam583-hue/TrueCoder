import asyncio

from truecoder.client.llm_client import LLMClient


async def main():
    client = LLMClient()
    messages = [{"role": "user", "content": "What's up"}]
    async for event in client.chat_completion(messages, True):
        print(event)
    print("done")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
