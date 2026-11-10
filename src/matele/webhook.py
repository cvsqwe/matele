"""Webhooks: messengers push updates to your HTTPS server instead of the
bot polling for them.

	dp = Dispatcher()
	webhook = Webhook(dp, base_url="https://bot.example.com")
	webhook.add(MaxBot(MAX_TOKEN), "/max")
	webhook.add(TelegramBot(TG_TOKEN), "/telegram")

	# any ASGI server:   uvicorn app:webhook --port 8080
	# or, with uvicorn installed (pip install matele[webhook]):
	webhook.run(port=8080)

`Webhook` is a plain ASGI application (no framework needed). On startup it
registers `base_url + path` with each messenger, with a random secret that
every request must carry; on shutdown it waits for the events still being
handled. Mount it in an existing ASGI app, or call `handle()` yourself
from any web framework.

Requests are answered with 200 right away and the events handled in the
background (messengers retry slow webhooks), in order per person. When
`max_pending` events are already waiting or being handled, new requests
get 503 until there is room: the messenger sends them again later,
instead of the process piling up tasks until it runs out of memory.

With `at_least_once=True` a request is answered only after its events
are handled. If the process dies in the middle, the messenger never got
its 200 and sends the update again after the restart, so the event is
not lost; handlers must cope with a repeat. A messenger that gives up
waiting on a slow handler and repeats the request while it still runs
gets its answer when that same handling finishes: the event is not
handled twice.

Requirements from the messengers: a public https URL with a valid
certificate; MAX accepts only port 443, Telegram 443, 80, 88 or 8443.
Put a reverse proxy (nginx, Caddy) in front if the app listens elsewhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from matele.dispatch.polling import KeyedLocks, drain
from matele.types import IncomingEvent, User, describe_update

if TYPE_CHECKING:
	from matele.dispatch.dispatcher import Dispatcher

logger = logging.getLogger(__name__)

MAX_PENDING = 1000  # events accepted but not handled yet
MAX_BODY = 4 * 1024 * 1024  # an update is a few KB; anything this big is not one
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")  # what both MAX and Telegram accept

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class WebhookTransport(Protocol):
	"""What a transport needs for webhooks (MaxBot and TelegramBot have it)."""

	platform: str
	title: str
	secret_header: str
	me: User | None

	async def get_me(self) -> User: ...

	async def close(self) -> None: ...

	async def set_webhook(self, url: str, *, secret: str | None = None) -> None: ...

	async def delete_webhook(self, url: str) -> None: ...

	async def parse_webhook(self, data: dict[str, Any]) -> list[IncomingEvent]: ...

	async def set_commands(self, commands: list[tuple[str, str]]) -> None: ...


class Webhook:
	def __init__(
		self,
		dp: Dispatcher,
		*,
		base_url: str | None = None,
		secret: str | None = None,
		delete_on_shutdown: bool = False,
		close_bots: bool = True,
		max_pending: int = MAX_PENDING,
		at_least_once: bool = False,
	):
		"""`base_url`: the public https address the routes are under; without
		it the webhooks are not registered on startup (do it yourself).
		`secret`: checked on every request; random by default.
		`max_pending`: how many accepted events may wait to be handled
		before new requests are answered 503 (and sent again later).
		`at_least_once`: answer a request only when its events are handled,
		so the messenger repeats an update the process died on."""
		if secret is not None and not _SECRET_RE.match(secret):
			raise ValueError("secret must be 5-256 characters: A-Z, a-z, 0-9, _ and -")
		self.dp = dp
		self.base_url = base_url.rstrip("/") if base_url else None
		self.secret = secret or secrets.token_urlsafe(32)
		self.delete_on_shutdown = delete_on_shutdown
		self.close_bots = close_bots
		self.max_pending = max(max_pending, 1)
		self.at_least_once = at_least_once
		# at_least_once: request (path + body) -> its handling, while it runs
		self._in_flight: dict[bytes, asyncio.Future[None]] = {}
		self._overloaded = False
		self.routes: dict[str, WebhookTransport] = {}
		self._locks = KeyedLocks()
		self._running: set[asyncio.Task[None]] = set()

	def add(self, bot: WebhookTransport, path: str) -> None:
		if not path.startswith("/"):
			raise ValueError(f"path must start with '/': {path!r}")
		if path in self.routes:
			raise ValueError(f"{path!r} is already used by {self.routes[path].title}")
		self.routes[path] = bot

	# ------------------------------------------------------------ lifecycle
	async def startup(self) -> None:
		"""Learns each bot's identity and registers the webhooks (if base_url is set)."""
		self.dp.freeze()
		for path, bot in self.routes.items():
			me = await bot.get_me()
			await self.dp.publish_commands(bot)
			if self.base_url:
				await bot.set_webhook(self.base_url + path, secret=self.secret)
				logger.info("%s: webhook for @%s at %s%s", bot.title, me.username, self.base_url, path)

	async def shutdown(self, timeout: float = 10.0) -> None:
		"""Waits (up to `timeout` s) for events being handled, then cancels the rest."""
		await drain(self._running, timeout)
		for path, bot in self.routes.items():
			if self.delete_on_shutdown and self.base_url:
				try:
					await bot.delete_webhook(self.base_url + path)
				except Exception:
					logger.exception("%s: could not delete the webhook", bot.title)
			if self.close_bots:
				await bot.close()
		await self.dp.storage.close()

	# ------------------------------------------------------------ requests
	async def handle(self, path: str, headers: Mapping[str, str], body: bytes) -> int:
		"""Handles one webhook request; returns the HTTP status to answer.
		`headers` keys are matched case-insensitively."""
		bot = self.routes.get(path)
		if bot is None:
			return 404
		lowered = {k.lower(): v for k, v in headers.items()}
		given = lowered.get(bot.secret_header.lower(), "")
		if not hmac.compare_digest(given.encode(), self.secret.encode()):
			logger.warning("%s: webhook request with a wrong secret", bot.title)
			return 403
		request_key = hashlib.sha256(path.encode() + b"\0" + body).digest() if self.at_least_once else b""
		repeated = self._in_flight.get(request_key)
		if repeated is not None:  # the messenger's retry of a request still being handled
			await asyncio.shield(repeated)
			return 200
		if len(self._running) >= self.max_pending:
			if not self._overloaded:  # once per overload, not per request
				self._overloaded = True
				logger.warning(
					"%s events are waiting to be handled: answering 503 until there is room", len(self._running)
				)
			return 503
		self._overloaded = False
		try:
			data = json.loads(body)
		except ValueError:
			return 400
		if not isinstance(data, dict):
			return 400
		try:
			events = await bot.parse_webhook(data)
		except Exception:  # a broken update must not make the messenger retry it forever
			logger.exception("%s: could not parse a webhook %s", bot.title, describe_update(data))
			return 200
		tasks: list[asyncio.Task[None]] = []
		for event in events:
			task = asyncio.create_task(self._feed(event))
			self._running.add(task)
			task.add_done_callback(self._running.discard)
			tasks.append(task)
		if self.at_least_once and tasks:
			handled = asyncio.ensure_future(_all_done(tasks))
			self._in_flight[request_key] = handled
			handled.add_done_callback(lambda _: self._in_flight.pop(request_key, None))
			# Shielded: the handling goes on even if this request is dropped.
			await asyncio.shield(handled)
		return 200

	async def _feed(self, event: IncomingEvent) -> None:
		key = self.dp.storage_key(event)
		async with self._locks.hold(key), self.dp.exclusive(key):
			await self.dp.feed(event)

	# ------------------------------------------------------------ ASGI
	async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
		if scope["type"] == "lifespan":
			await self._lifespan(receive, send)
			return
		if scope["type"] != "http":
			return
		# Mounted under a prefix (e.g. Starlette's Mount("/hooks", webhook)),
		# the path still includes it: routes are relative to the mount.
		path, root = scope["path"], scope.get("root_path", "")
		if root and path.startswith(root):
			path = path[len(root) :] or "/"
		if scope["method"] != "POST":
			await _respond(send, 405 if path in self.routes else 404)
			return
		body = bytearray()
		while True:
			message = await receive()
			body += message.get("body", b"")
			if len(body) > MAX_BODY:
				await _respond(send, 413)
				return
			if not message.get("more_body"):
				break
		headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
		await _respond(send, await self.handle(path, headers, bytes(body)))

	async def _lifespan(self, receive: Receive, send: Send) -> None:
		while True:
			message = await receive()
			if message["type"] == "lifespan.startup":
				try:
					await self.startup()
				except Exception as exc:
					logger.exception("Webhook startup failed")
					await send({"type": "lifespan.startup.failed", "message": str(exc)})
					return
				await send({"type": "lifespan.startup.complete"})
			elif message["type"] == "lifespan.shutdown":
				await self.shutdown()
				await send({"type": "lifespan.shutdown.complete"})
				return

	def run(self, host: str = "0.0.0.0", port: int = 8080, **uvicorn_options: Any) -> None:
		"""Serves the webhook with uvicorn (`pip install matele[webhook]`)."""
		try:
			import uvicorn
		except ImportError:
			raise RuntimeError("Webhook.run() needs uvicorn: pip install matele[webhook]") from None
		uvicorn.run(self, host=host, port=port, **uvicorn_options)


async def _all_done(tasks: list[asyncio.Task[None]]) -> None:
	await asyncio.wait(tasks)


async def _respond(send: Send, status: int) -> None:
	await send({"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0")]})
	await send({"type": "http.response.body", "body": b""})
