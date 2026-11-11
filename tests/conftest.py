"""Shared fixtures: retries in the API clients and the Hub never really sleep in tests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
	"""Tests that count the waits patch `_sleep` again on top of this."""

	async def instant(delay: float) -> None:
		await asyncio.sleep(0)

	monkeypatch.setattr("matele.telegram.api._sleep", instant)
	monkeypatch.setattr("matele.max.api._sleep", instant)
	monkeypatch.setattr("matele.hub._sleep", instant)
