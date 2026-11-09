"""TelegramApi: a thin async client for the whole Telegram Bot API.

	api = TelegramApi(token)
	me = await api.call("getMe")
	await api.call("sendMessage", chat_id=42, text="<b>hi</b>", parse_mode="HTML")
	await api.call("sendDocument", chat_id=42, document=InputFile(b"...", "report.csv"))

Any method of https://core.telegram.org/bots/api works through `call()`:
parameters are passed as keyword arguments (None values are dropped, dicts
and lists are sent as JSON), the `result` field is returned as plain JSON.
Files go as InputFile and switch the request to multipart.

Shared behaviour (same as MaxApi):
  • 429 is retried after `retry_after`, 502/503/504 with backoff;
  • errors raise matele.BotApiError (BotForbiddenError for 403);
  • a ConnectionGuard makes calls fail fast for a while after a network
	failure; getUpdates and getMe bypass it;
  • the bot token is never put into exception texts or logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import httpx

from matele.errors import BotApiError, BotForbiddenError, BotUnavailableError, ConnectionGuard
from matele.types import TELEGRAM

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.telegram.org"

RETRY_STATUSES = (502, 503, 504)
RETRY_DELAYS = (1.0, 2.0, 4.0)
FLOOD_RETRIES = 2
RETRY_AFTER_CAP = 60.0
_PROBES = {"getUpdates", "getMe", "deleteWebhook"}

_sleep = asyncio.sleep  # patched in tests


@dataclass
class InputFile:
	"""A file to upload: bytes, a path, or a binary file object (streamed)."""

	source: bytes | Path | IO[bytes]
	filename: str | None = None

	def name(self) -> str:
		if self.filename:
			return self.filename
		if isinstance(self.source, Path):
			return self.source.name
		return "file"


class _RedactTokens(logging.Filter):
	"""httpx logs every request URL at INFO, and Telegram's URLs contain the
	bot token: hide it before the record reaches any handler."""

	def __init__(self) -> None:
		super().__init__()
		self.tokens: set[str] = set()

	def filter(self, record: logging.LogRecord) -> bool:
		if not self.tokens:
			return True
		message = record.getMessage()
		redacted = message
		for token in self.tokens:
			redacted = redacted.replace(token, "***")
		if redacted != message:
			record.msg, record.args = redacted, ()
		return True


_redact = _RedactTokens()
for _name in ("httpx", "httpcore"):
	logging.getLogger(_name).addFilter(_redact)


def _rewindable(source: bytes | Path | IO[bytes]) -> bool:
	return isinstance(source, bytes | Path) or source.seekable()


def api_error(status: int, method: str, description: str) -> BotApiError:
	cls = BotForbiddenError if status == 403 else BotApiError
	return cls(status, method, description, platform=TELEGRAM)


class TelegramApi:
	def __init__(
		self,
		token: str,
		*,
		api_url: str = DEFAULT_API_URL,
		timeout: float = 35.0,
		proxy: str | None = None,
		http: httpx.AsyncClient | None = None,
		guard: ConnectionGuard | None = None,
	):
		if not token:
			raise ValueError("Telegram bot token is empty")
		_redact.tokens.add(token)
		api_url = api_url.rstrip("/")
		self._api_base = f"{api_url}/bot{token}/"
		self._file_base = f"{api_url}/file/bot{token}/"
		self._own_http = http is None
		self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), proxy=proxy)
		self.guard = guard or ConnectionGuard(TELEGRAM)

	async def close(self) -> None:
		if self._own_http:
			await self._http.aclose()

	async def __aenter__(self) -> TelegramApi:
		return self

	async def __aexit__(self, *exc: object) -> None:
		await self.close()

	async def call(self, method: str, *, request_timeout: float | None = None, **params: Any) -> Any:
		"""Calls any Bot API method; returns its `result`."""
		params = {k: v for k, v in params.items() if v is not None}
		files = {k: v for k, v in params.items() if isinstance(v, InputFile)}
		probe = method in _PROBES
		flood_left, retry = FLOOD_RETRIES, 0
		# A stream that can't be rewound can't be sent twice: no retries then.
		repeatable = all(_rewindable(f.source) for f in files.values())
		while True:
			resp = await self._post(method, params, files, request_timeout, probe)
			try:
				payload = resp.json()
			except ValueError:
				payload = {"ok": False, "description": resp.text[:200]}
			if not isinstance(payload, dict):
				payload = {"ok": False, "description": "unexpected answer"}
			if payload.get("ok"):
				return payload.get("result")
			status = int(payload.get("error_code") or resp.status_code)
			description = str(payload.get("description") or "unknown error")
			retry_after = (payload.get("parameters") or {}).get("retry_after")
			if not repeatable:
				raise api_error(status, method, description)
			if status == 429 and retry_after and flood_left > 0:
				flood_left -= 1
				delay = min(float(retry_after), RETRY_AFTER_CAP)
				logger.info("Telegram flood control on %s, waiting %ss", method, delay)
				await _sleep(delay)
				continue
			if status in RETRY_STATUSES and retry < len(RETRY_DELAYS):
				await _sleep(RETRY_DELAYS[retry])
				retry += 1
				continue
			raise api_error(status, method, description)

	async def _post(
		self,
		method: str,
		params: dict[str, Any],
		files: dict[str, InputFile],
		timeout: float | None,
		probe: bool,
	) -> httpx.Response:
		if not probe:
			self.guard.check()
		opened: list[IO[bytes]] = []
		try:
			if files:
				data = {
					k: v if isinstance(v, str) else json.dumps(v)
					for k, v in params.items()
					if not isinstance(v, InputFile)
				}
				multipart: dict[str, Any] = {}
				for key, f in files.items():
					source = f.source
					if isinstance(source, Path):
						source = source.open("rb")
						opened.append(source)
					elif not isinstance(source, bytes) and source.seekable():
						source.seek(0)  # a retry re-sends the whole file
					mime = mimetypes.guess_type(f.name())[0] or "application/octet-stream"
					multipart[key] = (f.name(), source, mime)
				resp = await self._http.post(
					self._api_base + method, data=data, files=multipart, timeout=timeout or 120.0
				)
			else:
				resp = await self._http.post(
					self._api_base + method,
					json=params,
					timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
				)
		except httpx.TransportError as exc:
			self.guard.failed()
			# str(exc) may echo the request URL, which contains the token
			raise BotUnavailableError(0, "network", type(exc).__name__, platform=TELEGRAM) from None
		finally:
			for fh in opened:
				fh.close()
		self.guard.ok()
		return resp

	async def download(self, file_path: str) -> bytes:
		"""Downloads a file by the `file_path` from getFile (up to 20 MB on the public API)."""
		self.guard.check()
		try:
			resp = await self._http.get(self._file_base + file_path, timeout=120.0)
		except httpx.TransportError as exc:
			self.guard.failed()
			raise BotUnavailableError(0, "network", type(exc).__name__, platform=TELEGRAM) from None
		if resp.status_code >= 400:
			raise api_error(resp.status_code, "download", f"HTTP {resp.status_code}")
		return resp.content
