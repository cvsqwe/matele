"""A scripted Telegram Bot API for tests: httpx.MockTransport + recorded calls."""

from __future__ import annotations

import json
from typing import Any

import httpx

from matele.telegram import TelegramApi

TOKEN = "123:SECRET"


class FakeTelegram:
	"""Scripted Bot API: method -> queued answers (dict = result, or full JSON / exception)."""

	def __init__(self) -> None:
		self.calls: list[tuple[str, Any, httpx.Request]] = []
		self.answers: dict[str, list[Any]] = {}

	def ok(self, method: str, *results: Any) -> None:
		self.answers.setdefault(method, []).extend({"ok": True, "result": r} for r in results)

	def fail(self, method: str, code: int, description: str, **extra: Any) -> None:
		self.answers.setdefault(method, []).append(
			{"ok": False, "error_code": code, "description": description, **extra}
		)

	def raise_(self, method: str, exc: Exception) -> None:
		self.answers.setdefault(method, []).append(exc)

	def handle(self, request: httpx.Request) -> httpx.Response:
		path = request.url.path
		if "/file/" in path:
			return httpx.Response(200, content=b"FILE:" + path.rsplit("/", 1)[-1].encode())
		method = path.rsplit("/", 1)[-1]
		content_type = request.headers.get("content-type", "")
		body: Any = json.loads(request.content) if content_type.startswith("application/json") else request.read()
		self.calls.append((method, body, request))
		queue = self.answers.get(method) or []
		if not queue:
			return httpx.Response(200, json={"ok": True, "result": True})
		answer = queue.pop(0)
		if isinstance(answer, Exception):
			raise answer
		return httpx.Response(200 if answer["ok"] else answer["error_code"], json=answer)

	def api(self) -> TelegramApi:
		return TelegramApi(
			TOKEN, api_url="https://tg.test", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
		)

	def methods(self) -> list[str]:
		return [c[0] for c in self.calls]


def tg_message(text: str | None = "hi", *, chat_type="private", message_id=10, chat_id=7, **extra) -> dict[str, Any]:
	return {
		"message_id": message_id,
		"chat": {"id": chat_id, "type": chat_type},
		"from": {"id": chat_id, "first_name": "Ann", "last_name": "Lee", "username": "ann"},
		"text": text,
		**extra,
	}
