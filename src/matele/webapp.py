"""Mini apps: checking that a request really comes from a person in MAX or
Telegram.

A mini app (web app) opened from the bot gets `initData` from the
messenger: a query string with the person's data, signed with the bot
token. Send it to your server with every request and check it there:

	data = bot.verify_init_data(request.headers["X-Init-Data"])
	await bot.send_message("Заказ принят", user_id=data.user.id)

Never trust user ids that the page sends on its own: without the signature
check anyone can claim to be anyone.

MAX and Telegram sign initData the same way: the secret key is
HMAC-SHA256(key="WebAppData", message=bot_token), the signature is
HMAC-SHA256(key=secret, message=all fields but `hash`, sorted by name,
as "name=value" lines). See https://dev.max.ru/docs/webapps/validation and
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from matele.types import User

DEFAULT_MAX_AGE = 3600  # seconds; MAX recommends treating initData as valid for an hour


class InitDataError(ValueError):
	"""initData is malformed, has a wrong signature, or is too old."""


@dataclass
class WebAppData:
	user: User | None
	auth_date: int  # unix time the messenger signed the data
	start_param: str | None = None  # the `app=` value of the button that opened the app
	language: str | None = None  # the person's interface language, e.g. "ru"
	query_id: str | None = None
	chat_type: str | None = None
	fields: dict[str, str] = field(default_factory=dict)  # every signed field, decoded


def verify_init_data(
	init_data: str,
	token: str,
	*,
	platform: str,
	max_age: float | None = DEFAULT_MAX_AGE,
	now: float | None = None,
) -> WebAppData:
	"""Checks the signature (and age, unless max_age=None) of `init_data`
	from a mini app; returns its contents or raises InitDataError.

	`init_data` is `window.Telegram.WebApp.initData` / `window.WebApp.initData`
	as is. A MAX launch fragment ("WebAppData=...&WebAppPlatform=...") is
	accepted too."""
	pairs = parse_qsl(init_data.lstrip("#?"), keep_blank_values=True, strict_parsing=False)
	wrapped = [v for k, v in pairs if k == "WebAppData"]
	if wrapped:  # MAX launch fragment: the signed data is inside WebAppData
		if len(wrapped) != 1:
			raise InitDataError("WebAppData appears more than once")
		pairs = parse_qsl(wrapped[0], keep_blank_values=True)
	fields: dict[str, str] = {}
	for key, value in pairs:
		if key in fields:
			raise InitDataError(f"field {key!r} appears more than once")
		fields[key] = value
	given = fields.pop("hash", None)
	if not given:
		raise InitDataError("no hash")

	check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
	secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
	expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, given.lower()):
		raise InitDataError("wrong signature")

	try:
		auth_date = int(fields["auth_date"])
	except (KeyError, ValueError):
		raise InitDataError("no valid auth_date") from None
	if max_age is not None and (now if now is not None else time.time()) - auth_date > max_age:
		raise InitDataError("initData is too old")

	user_data = _json(fields.get("user"))
	user = None
	if user_data and "id" in user_data:
		name = " ".join(str(p) for p in (user_data.get("first_name"), user_data.get("last_name")) if p) or None
		user = User(id=int(user_data["id"]), platform=platform, name=name, username=user_data.get("username"))
	return WebAppData(
		user=user,
		auth_date=auth_date,
		start_param=fields.get("start_param") or None,
		language=(user_data or {}).get("language_code"),
		query_id=fields.get("query_id"),
		chat_type=fields.get("chat_type"),
		fields=fields,
	)


def _json(value: str | None) -> dict[str, Any] | None:
	if not value:
		return None
	try:
		data = json.loads(value)
	except ValueError:
		raise InitDataError("a field holds broken JSON") from None
	return data if isinstance(data, dict) else None
