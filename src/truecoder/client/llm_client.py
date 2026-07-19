import os
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()


class LLMClient:
    def __init__(self) -> None:
        self.__client: AsyncOpenAI | None = None

    def get_client(self) -> AsyncOpenAI:
        if self.__client is None:
            api_key = os.getenv("API_KEY")
            base_url = os.getenv("BASE_URL")
            if not api_key or not base_url:
                raise RuntimeError("API_KEY and BASE_URL must be set in the .env file")
            self.__client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self.__client

    async def close(self) -> None:
        if self.__client:
            await self.__client.close()
            self.__client = None

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        stream: bool = True,
    ):
        client = self.get_client()
        keywargs = {
            "model": os.getenv("MODEL"),
            "messages": messages,
            "stream": stream,
        }
        if stream:
            await self._stream_response()
        else:
            await self._non_stream_reponse(client, keywargs)

    async def _stream_response(self):
        pass

    async def _non_stream_reponse(self, client: AsyncOpenAI, keywargs: dict[str, Any]):
        response = await client.chat.completions.create(**keywargs)
        print(response)
