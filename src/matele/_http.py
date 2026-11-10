"""HTTP helpers shared by the transports."""

from __future__ import annotations

import httpx

# Files people send are downloaded into memory, so their size is capped:
# without a cap one huge file would take the whole process down.
DEFAULT_MAX_DOWNLOAD = 50 * 1024 * 1024


def is_connection_failure(exc: Exception) -> bool:
	"""Does this failure say the API can't be reached (refused, reset or
	timed-out connection, DNS, proxy)? A timeout while reading, writing or
	waiting for a free connection doesn't: the API was reached, this one
	call was slow or the client is busy."""
	return isinstance(exc, httpx.TransportError) and not isinstance(
		exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout
	)


def never_sent(exc: Exception) -> bool:
	"""Did the request surely not reach the API (no connection could be
	made, or none was free)? After anything else (a read timeout, a
	connection dropped mid-way) the API may have done what was asked."""
	return isinstance(
		exc,
		httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout | httpx.ProxyError | httpx.UnsupportedProtocol,
	)


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
