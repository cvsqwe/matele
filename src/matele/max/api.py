"""MaxApi: a typed async client for the whole MAX Bot API.

	api = MaxApi(token)
	me = await api.get_me()
	msg = await api.send_message(user_id=42, text="<b>Привет</b>", format="html")
	await api.close()

Every method maps to one endpoint of https://dev.max.ru/docs-api and
returns the models from matele.max.models. Behaviour shared by all calls:
  • TLS trusts the Russian Trusted Root CA (bundled), which MAX's API is
	signed with, on top of certifi's public roots, for this client only;
  • 429 and 502/503/504 are retried (honouring Retry-After), then raised;
  • errors raise matele.BotApiError (BotForbiddenError when the person
	blocked the bot or never opened the dialog); a 200 answer with
	`"success": false` raises too;
  • after a network failure, calls fail fast for a cooldown
	(ConnectionGuard) instead of each waiting for its own timeout; long
	polling bypasses the guard and closes it when MAX answers again.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import IO, Any

import certifi
import httpx

from matele._http import DEFAULT_MAX_DOWNLOAD, TooLargeError, read_limited
from matele.errors import BotApiError, BotForbiddenError, BotUnavailableError, ConnectionGuard
from matele.max import models as m
from matele.types import MAX

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://platform-api2.max.ru"

RETRY_STATUSES = (429, 502, 503, 504)
RETRY_DELAYS = (1.0, 2.0, 4.0)
RETRY_AFTER_CAP = 30.0
# A freshly uploaded file needs a moment before it can be attached.
ATTACHMENT_NOT_READY = "attachment.not.ready"
ATTACHMENT_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0)

# 404 codes meaning "this person can't be written to" (never opened the
# dialog, or it is gone): retrying won't help.
_NO_DIALOG = {"dialog.not.found", "chat.not.found", "user.not.found"}

FileSource = bytes | Path | IO[bytes]

_sleep = asyncio.sleep  # patched in tests


def ssl_context() -> ssl.SSLContext:
	"""certifi's roots + the Russian Trusted Root CA, for MAX hosts only."""
	ctx = ssl.create_default_context(cafile=certifi.where())
	cert = resources.files("matele.max").joinpath("certs/russian_trusted_root_ca.pem")
	ctx.load_verify_locations(cadata=cert.read_text(encoding="ascii"))
	return ctx


def api_error(status: int, code: str, message: str) -> BotApiError:
	forbidden = status == 403 or (status == 404 and code in _NO_DIALOG)
	return (BotForbiddenError if forbidden else BotApiError)(status, code, message, platform=MAX)


def _retry_after(resp: httpx.Response) -> float | None:
	try:
		value = float(resp.headers.get("Retry-After", ""))
	except ValueError:
		return None
	return min(value, RETRY_AFTER_CAP) if value > 0 else None


def _csv(values: Iterable[Any] | None) -> str | None:
	return ",".join(str(v) for v in values) if values is not None else None


class MaxApi:
	def __init__(
		self,
		token: str,
		*,
		base_url: str = DEFAULT_BASE_URL,
		timeout: float = 35.0,
		http: httpx.AsyncClient | None = None,
		files_http: httpx.AsyncClient | None = None,
		guard: ConnectionGuard | None = None,
		max_download: int | None = DEFAULT_MAX_DOWNLOAD,
	):
		"""`http` / `files_http`: bring your own clients (proxies, tests).
		File hosts (uploads, downloads) never get the bot token.
		`max_download`: the biggest file download() reads, in bytes (50 MB
		by default; None for no limit)."""
		if not token:
			raise ValueError("MAX bot token is empty")
		self._own_http = http is None
		ctx = ssl_context() if http is None or files_http is None else None
		self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), verify=ctx)  # type: ignore[arg-type]
		self._files = files_http or httpx.AsyncClient(timeout=120.0, verify=ctx, follow_redirects=True)  # type: ignore[arg-type]
		self._own_files = files_http is None
		self._base_url = base_url.rstrip("/")
		self._headers = {"Authorization": token}
		self.guard = guard or ConnectionGuard(MAX)
		self.max_download = max_download

	async def close(self) -> None:
		if self._own_http:
			await self._http.aclose()
		if self._own_files:
			await self._files.aclose()

	async def __aenter__(self) -> MaxApi:
		return self

	async def __aexit__(self, *exc: object) -> None:
		await self.close()

	# ------------------------------------------------------------------ transport
	async def request(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json: Any = None,
		timeout: float | None = None,
		probe: bool = False,
	) -> Any:
		"""One API call; returns the decoded JSON. Use it for endpoints this
		client doesn't cover yet. `probe` bypasses (and resets) the guard."""
		query = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}
		for attempt in range(len(RETRY_DELAYS) + 1):
			if not probe:
				self.guard.check()
			try:
				resp = await self._http.request(
					method,
					self._base_url + path,
					params=query,
					json=json,
					headers=self._headers,
					timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
				)
			except httpx.TransportError as exc:
				self.guard.failed()
				raise BotUnavailableError(0, "network", f"{type(exc).__name__}: {exc}", platform=MAX) from None
			self.guard.ok()
			if resp.status_code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
				delay = _retry_after(resp) or RETRY_DELAYS[attempt]
				logger.info("MAX answered %s on %s %s, retrying in %ss", resp.status_code, method, path, delay)
				await _sleep(delay)
				continue
			if resp.status_code >= 400:
				try:
					err = resp.json()
				except ValueError:
					err = {}
				if not isinstance(err, dict):
					err = {}
				raise api_error(
					resp.status_code,
					str(err.get("code") or ""),
					str(err.get("message") or err.get("error") or resp.text[:500]),
				)
			try:
				data = resp.json() if resp.content else {}
			except ValueError:  # a proxy's or a captive portal's page instead of the API
				raise api_error(resp.status_code, "bad.response", f"not JSON: {resp.text[:200]!r}") from None
			if isinstance(data, dict) and data.get("success") is False:
				raise api_error(resp.status_code, "request.failed", str(data.get("message") or "success: false"))
			return data
		raise AssertionError("unreachable")  # pragma: no cover

	# ------------------------------------------------------------------ bots
	async def get_me(self) -> m.BotInfo:
		"""The bot itself (GET /me). Bypasses the guard: a safe probe."""
		return m.BotInfo.from_dict(await self.request("GET", "/me", probe=True))

	async def set_my_commands(self, commands: Iterable[m.BotCommand]) -> m.BotCommandsInfo:
		"""Replaces the command list shown in clients (PATCH /me/commands); at most 32."""
		body = m.BotCommandsPatch(commands=list(commands)).to_dict()
		return m.BotCommandsInfo.from_dict(await self.request("PATCH", "/me/commands", json=body))

	# ------------------------------------------------------------------ chats
	async def get_chat(self, chat_id: int) -> m.Chat:
		return m.Chat.from_dict(await self.request("GET", f"/chats/{chat_id}"))

	async def edit_chat(
		self,
		chat_id: int,
		*,
		title: str | None = None,
		icon: m.PhotoAttachmentRequestPayload | None = None,
		pin: str | None = None,
		notify: bool | None = None,
	) -> m.Chat:
		"""Changes title / icon / pinned message (PATCH /chats/{chatId}); bot must be an admin."""
		body = m.ChatPatch(title=title, icon=icon, pin=pin, notify=notify).to_dict()
		return m.Chat.from_dict(await self.request("PATCH", f"/chats/{chat_id}", json=body))

	async def send_action(self, chat_id: int, action: m.SenderAction) -> None:
		"""Shows "typing…" and the like (POST /chats/{chatId}/actions)."""
		await self.request("POST", f"/chats/{chat_id}/actions", json={"action": action})

	async def get_pinned_message(self, chat_id: int) -> m.Message | None:
		data = await self.request("GET", f"/chats/{chat_id}/pin")
		return m.GetPinnedMessageResult.from_dict(data).message

	async def pin_message(self, chat_id: int, message_id: str, *, notify: bool | None = None) -> None:
		body = m.PinMessageBody(message_id=message_id, notify=notify).to_dict()
		await self.request("PUT", f"/chats/{chat_id}/pin", json=body)

	async def unpin_message(self, chat_id: int) -> None:
		await self.request("DELETE", f"/chats/{chat_id}/pin")

	async def get_membership(self, chat_id: int) -> m.ChatMember:
		"""The bot's own membership in a chat."""
		return m.ChatMember.from_dict(await self.request("GET", f"/chats/{chat_id}/members/me"))

	async def leave_chat(self, chat_id: int) -> None:
		await self.request("DELETE", f"/chats/{chat_id}/members/me")

	async def get_admins(self, chat_id: int) -> m.ChatMembersList:
		return m.ChatMembersList.from_dict(await self.request("GET", f"/chats/{chat_id}/members/admins"))

	async def add_admins(self, chat_id: int, admins: Iterable[m.ChatAdmin]) -> None:
		body = m.ChatAdminsList(admins=list(admins)).to_dict()
		await self.request("POST", f"/chats/{chat_id}/members/admins", json=body)

	async def remove_admin(self, chat_id: int, user_id: int) -> None:
		await self.request("DELETE", f"/chats/{chat_id}/members/admins/{user_id}")

	async def get_members(
		self,
		chat_id: int,
		*,
		user_ids: Iterable[int] | None = None,
		marker: int | None = None,
		count: int | None = None,
	) -> m.ChatMembersList:
		"""A page of members; pass the returned `marker` for the next page."""
		params = {"user_ids": _csv(user_ids), "marker": marker, "count": count}
		return m.ChatMembersList.from_dict(await self.request("GET", f"/chats/{chat_id}/members", params=params))

	async def add_members(self, chat_id: int, user_ids: Iterable[int]) -> m.ModifyMembersResult:
		body = m.UserIdsList(user_ids=list(user_ids)).to_dict()
		data = await self.request("POST", f"/chats/{chat_id}/members", json=body)
		return m.ModifyMembersResult.from_dict(data)

	async def remove_member(self, chat_id: int, user_id: int, *, block: bool | None = None) -> None:
		params = {"user_id": user_id, "block": block}
		await self.request("DELETE", f"/chats/{chat_id}/members", params=params)

	# ------------------------------------------------------------------ webhooks & updates
	async def get_subscriptions(self) -> list[m.Subscription]:
		return m.GetSubscriptionsResult.from_dict(await self.request("GET", "/subscriptions")).subscriptions

	async def subscribe(
		self, url: str, *, update_types: Iterable[str] | None = None, secret: str | None = None
	) -> None:
		"""Delivers updates to `url` (a webhook) instead of long polling.
		MAX sends `secret` in the X-Max-Bot-Api-Secret header."""
		body = m.SubscriptionRequestBody(
			url=url, update_types=list(update_types) if update_types is not None else None, secret=secret
		).to_dict()
		await self.request("POST", "/subscriptions", json=body)

	async def unsubscribe(self, url: str) -> None:
		await self.request("DELETE", "/subscriptions", params={"url": url})

	async def get_updates(
		self,
		*,
		marker: int | None = None,
		limit: int | None = None,
		timeout: int = 30,
		types: Iterable[str] | None = None,
	) -> m.UpdateList:
		"""Long polling (GET /updates): waits up to `timeout` seconds (≤ 90).
		Pass the returned `marker` to the next call. Bypasses the guard."""
		params = {"marker": marker, "limit": limit, "timeout": timeout, "types": _csv(types)}
		data = await self.request("GET", "/updates", params=params, timeout=timeout + 10, probe=True)
		return m.UpdateList.from_dict(data)

	# ------------------------------------------------------------------ messages
	async def get_messages(
		self,
		*,
		chat_id: int | None = None,
		message_ids: Iterable[str] | None = None,
		before: int | None = None,
		after: int | None = None,
		count: int | None = None,
	) -> list[m.Message]:
		"""Messages of a chat (newest first), or the given ones by id."""
		params = {
			"chat_id": chat_id,
			"message_ids": _csv(message_ids),
			"before": before,
			"after": after,
			"count": count,
		}
		return m.MessageList.from_dict(await self.request("GET", "/messages", params=params)).messages

	async def get_message(self, message_id: str) -> m.Message:
		return m.Message.from_dict(await self.request("GET", f"/messages/{message_id}"))

	async def send_message(
		self,
		*,
		user_id: int | None = None,
		chat_id: int | None = None,
		text: str | None = None,
		attachments: Sequence[m.AttachmentRequest] | None = None,
		format: m.TextFormat | None = None,
		link: m.NewMessageLink | None = None,
		notify: bool | None = None,
		disable_link_preview: bool | None = None,
	) -> m.Message:
		"""Sends to a person (`user_id`) or a chat (`chat_id`). A just-uploaded
		attachment that isn't processed yet is retried for a few seconds."""
		if (user_id is None) == (chat_id is None):
			raise ValueError("send_message() needs exactly one of user_id, chat_id")
		body = m.NewMessageBody(
			text=text,
			attachments=list(attachments) if attachments is not None else None,
			format=format,
			link=link,
			notify=notify,
		)
		params = {"user_id": user_id, "chat_id": chat_id, "disable_link_preview": disable_link_preview}
		data = await self._retry_not_ready("POST", "/messages", params, body.to_dict())
		return m.SendMessageResult.from_dict(data).message

	async def edit_message(
		self,
		message_id: str,
		*,
		text: str | None = None,
		attachments: Sequence[m.AttachmentRequest] | None = None,
		format: m.TextFormat | None = None,
		link: m.NewMessageLink | None = None,
		notify: bool | None = None,
	) -> None:
		"""Edits a message the bot sent. Fields left at None stay as they
		are; `attachments=[]` removes all attachments (and the keyboard)."""
		body = m.NewMessageBody(
			text=text,
			attachments=list(attachments) if attachments is not None else None,
			format=format,
			link=link,
			notify=notify,
		)
		await self._retry_not_ready("PUT", "/messages", {"message_id": message_id}, body.to_dict())

	async def delete_message(self, message_id: str) -> None:
		await self.request("DELETE", "/messages", params={"message_id": message_id})

	async def answer_callback(
		self,
		callback_id: str,
		*,
		notification: str | None = None,
		message: m.NewMessageBody | None = None,
	) -> None:
		"""Answers a button press: a one-time `notification` toast, and/or a
		`message` that replaces the one the button is on."""
		body = m.CallbackAnswer(notification=notification, message=message).to_dict()
		await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

	async def get_video(self, video_token: str) -> m.VideoAttachmentDetails:
		"""Playback URLs and details of a video attachment."""
		return m.VideoAttachmentDetails.from_dict(await self.request("GET", f"/videos/{video_token}"))

	async def _retry_not_ready(self, method: str, path: str, params: dict[str, Any], body: dict[str, Any]) -> Any:
		for delay in (*ATTACHMENT_RETRY_DELAYS, None):
			try:
				return await self.request(method, path, params=params, json=body)
			except BotApiError as exc:
				if exc.code != ATTACHMENT_NOT_READY or delay is None:
					raise
				await _sleep(delay)
		raise AssertionError("unreachable")  # pragma: no cover

	# ------------------------------------------------------------------ comments (channel posts)
	async def get_comments(
		self,
		message_id: str,
		*,
		comment_ids: Iterable[str] | None = None,
		before: int | None = None,
		after: int | None = None,
		count: int | None = None,
	) -> list[m.CommentMessage]:
		params = {"comment_ids": _csv(comment_ids), "before": before, "after": after, "count": count}
		data = await self.request("GET", f"/messages/{message_id}/comments", params=params)
		return m.CommentMessageList.from_dict(data).messages

	async def get_comment(self, message_id: str, comment_id: str) -> m.CommentMessage:
		return m.CommentMessage.from_dict(await self.request("GET", f"/messages/{message_id}/comments/{comment_id}"))

	async def send_comment(
		self,
		message_id: str,
		*,
		text: str | None = None,
		format: m.TextFormat | None = None,
		link: m.NewMessageLink | None = None,
		disable_link_preview: bool | None = None,
	) -> m.CommentMessage:
		body = m.NewCommentBody(text=text, format=format, link=link).to_dict()
		params = {"disable_link_preview": disable_link_preview}
		data = await self.request("POST", f"/messages/{message_id}/comments", params=params, json=body)
		return m.SendCommentResult.from_dict(data).message

	async def edit_comment(
		self,
		message_id: str,
		comment_id: str,
		*,
		text: str | None = None,
		format: m.TextFormat | None = None,
		link: m.NewMessageLink | None = None,
	) -> None:
		body = m.NewCommentBody(text=text, format=format, link=link).to_dict()
		await self.request("PUT", f"/messages/{message_id}/comments", params={"comment_id": comment_id}, json=body)

	async def delete_comment(self, message_id: str, comment_id: str) -> None:
		await self.request("DELETE", f"/messages/{message_id}/comments", params={"comment_id": comment_id})

	# ------------------------------------------------------------------ files
	async def get_upload_url(self, type: m.UploadType) -> m.UploadEndpoint:
		return m.UploadEndpoint.from_dict(await self.request("POST", "/uploads", params={"type": type}))

	async def upload(self, source: FileSource, *, type: m.UploadType, filename: str = "file") -> m.AttachmentRequest:
		"""Uploads a file and returns the attachment to put in `attachments=`.
		`source` is bytes, a path, or a binary file object (streamed, never
		read into memory whole). The upload host doesn't get the bot token."""
		endpoint = await self.get_upload_url(type)
		if isinstance(source, Path):
			with source.open("rb") as fh:
				result = await self._post_file(endpoint.url, fh, source.name if filename == "file" else filename)
		else:
			result = await self._post_file(endpoint.url, source, filename)
		if type == "image":
			if isinstance(result.get("photos"), dict):
				photos = {k: m.PhotoToken(token=v["token"]) for k, v in result["photos"].items()}
				return m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(photos=photos))
			return m.PhotoAttachmentRequest(payload=m.PhotoAttachmentRequestPayload(token=_find_token(result)))
		# video/audio: the token comes with the upload URL; file: with the upload answer
		token = endpoint.token if type in ("video", "audio") and endpoint.token else _find_token(result)
		cls = {"video": m.VideoAttachmentRequest, "audio": m.AudioAttachmentRequest}.get(type, m.FileAttachmentRequest)
		return cls(payload=m.UploadedInfo(token=token))

	async def _post_file(self, url: str, data: bytes | IO[bytes], filename: str) -> dict[str, Any]:
		self.guard.check()
		try:
			resp = await self._files.post(url, files={"data": (filename, data)})
		except httpx.TransportError as exc:
			self.guard.failed()
			raise BotUnavailableError(0, "network", f"{type(exc).__name__}: {exc}", platform=MAX) from None
		self.guard.ok()
		if resp.status_code >= 400:
			raise api_error(resp.status_code, "upload.failed", resp.text[:500])
		try:
			result = resp.json()
		except ValueError:
			result = {}
		return result if isinstance(result, dict) else {}

	async def download(self, url: str, *, max_size: int | None = -1) -> bytes:
		"""Downloads an attachment by its URL (`attachment.payload.url`).
		The file is read into memory, so a bigger one than `max_size` bytes
		(by default the client's `max_download`; None for no limit) is
		refused with a 413 BotApiError instead."""
		limit = self.max_download if max_size == -1 else max_size
		try:
			async with self._files.stream("GET", url) as resp:
				if resp.status_code >= 400:
					await resp.aread()
					raise api_error(resp.status_code, "download.failed", resp.text[:200])
				try:
					return await read_limited(resp, limit)
				except TooLargeError:
					raise api_error(413, "download.too_large", f"the file is over {limit} bytes") from None
		except httpx.TransportError as exc:
			raise BotUnavailableError(0, "network", f"{type(exc).__name__}: {exc}", platform=MAX) from None


def _find_token(result: dict[str, Any]) -> str:
	"""The upload answer has the token at the top, or under a per-type key."""
	token = result.get("token")
	if not token:
		token = next((v["token"] for v in result.values() if isinstance(v, dict) and v.get("token")), None)
	if not token:
		raise api_error(200, "upload.bad_response", f"no token in the upload answer: {result!r}"[:500])
	return str(token)
