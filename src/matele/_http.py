"""HTTP helpers shared by the transports."""

from __future__ import annotations

import httpx

# Files people send are downloaded into memory, so their size is capped:
# without a cap one huge file would take the whole process down.
DEFAULT_MAX_DOWNLOAD = 50 * 1024 * 1024


class TooLargeError(Exception):
	"""The body is bigger than the limit."""


async def read_limited(resp: httpx.Response, limit: int | None) -> bytes:
	"""Reads a streamed response, giving up as soon as it is over `limit`
	bytes (None: no limit). Raises TooLargeError."""
	declared = resp.headers.get("content-length", "")
	if limit is not None and declared.isdigit() and int(declared) > limit:
		raise TooLargeError
	chunks: list[bytes] = []
	size = 0
	async for chunk in resp.aiter_bytes():
		size += len(chunk)
		if limit is not None and size > limit:
			raise TooLargeError
		chunks.append(chunk)
	return b"".join(chunks)
