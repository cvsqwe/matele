"""A scripted MAX server for tests: httpx.MockTransport + recorded requests."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import httpx

from matele.max import MaxApi

BASE = "https://max.test"


@dataclass
class Call:
	method: str
	path: str
	params: dict[str, str]
	json: Any
	headers: httpx.Headers
	content: bytes


class FakeMaxServer:
	def __init__(self) -> None:
		self.calls: list[Call] = []
		self.files: list[Call] = []
		self._routes: dict[tuple[str, str], deque[httpx.Response | Exception]] = defaultdict(deque)
		self._defaults: dict[tuple[str, str], httpx.Response] = {}

	def on(self, method: str, path: str, *responses: Any, status: int = 200, headers: dict | None = None) -> None:
		"""Queues responses (dicts become JSON; exceptions are raised)."""
		for r in responses:
			self._routes[(method, path)].append(
				r if isinstance(r, Exception | httpx.Response) else httpx.Response(status, json=r, headers=headers)
			)

	def always(self, method: str, path: str, body: Any, status: int = 200) -> None:
		self._defaults[(method, path)] = httpx.Response(status, json=body)

	def api(self, **kwargs: Any) -> MaxApi:
		return MaxApi(
			"secret-token",
			base_url=BASE,
			http=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
			files_http=httpx.AsyncClient(transport=httpx.MockTransport(self._handle_files)),
			**kwargs,
		)

	def _handle(self, request: httpx.Request) -> httpx.Response:
		body = json.loads(request.content) if request.content else None
		call = Call(request.method, request.url.path, dict(request.url.params), body, request.headers, request.content)
		self.calls.append(call)
		key = (request.method, request.url.path)
		if self._routes.get(key):
			r = self._routes[key].popleft()
			if isinstance(r, Exception):
				raise r
			return r
		if key in self._defaults:
			return self._defaults[key]
		return httpx.Response(404, json={"code": "not.found", "message": f"no route {key}"})

	def _handle_files(self, request: httpx.Request) -> httpx.Response:
		self.files.append(Call(request.method, str(request.url), {}, None, request.headers, request.read()))
		key = (request.method, str(request.url))
		if self._routes.get(key):
			r = self._routes[key].popleft()
			if isinstance(r, Exception):
				raise r
			return r
		return httpx.Response(404, text="no file")


# --------------------------------------------------------------------- sample payloads
def user(user_id: int = 7, first_name: str = "Ann", **extra: Any) -> dict[str, Any]:
	return {"user_id": user_id, "first_name": first_name, "is_bot": False, **extra}


def message(
	text: str | None = "hi",
	*,
	mid: str = "mid.1",
	sender: dict | None = None,
	chat_type: str = "dialog",
	chat_id: int = 100,
	attachments: list | None = None,
) -> dict[str, Any]:
	body: dict[str, Any] = {"mid": mid, "seq": 1, "text": text}
	if attachments is not None:
		body["attachments"] = attachments
	return {
		"sender": sender if sender is not None else user(),
		"recipient": {"chat_id": chat_id, "chat_type": chat_type, "user_id": None},
		"timestamp": 1700000000000,
		"body": body,
	}
