"""initData signed the way MAX and Telegram sign it, for mini app tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import quote, urlencode

from tests.telegram_fake import TOKEN

NOW = 1_800_000_000


def sign(fields: dict[str, str], token: str) -> str:
	"""Signs like the messengers do (dev.max.ru/docs/webapps/validation):
	secret = HMAC_SHA256(key="WebAppData", msg=token); hash = HMAC_SHA256(secret,
	sorted "k=v" lines joined by newlines), as lowercase hex."""
	lines = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
	secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
	return hmac.new(secret, lines.encode(), hashlib.sha256).hexdigest()


def init_data(token: str = TOKEN, **overrides: str) -> str:
	fields = {
		"query_id": "AAH",
		"user": json.dumps(
			{"id": 42, "first_name": "Аня", "last_name": "Ли", "username": "anya", "language_code": "ru"},
			ensure_ascii=False,
		),
		"auth_date": str(NOW - 60),
		"start_param": "orders",
		"signature": "ed25519-base64-would-be-here",
	}
	fields.update(overrides)
	return urlencode({**fields, "hash": sign(fields, token)}, quote_via=quote)
