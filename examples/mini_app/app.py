"""A mini app for MAX and Telegram: the bot sends a button, the button opens
index.html, the page sends an order to this server, the server checks who
sent it and confirms through the bot.

	pip install "matele @ git+https://github.com/cvsqwe/matele" fastapi uvicorn
	APP_URL=https://your.domain/ MAX_BOT_TOKEN=... TELEGRAM_BOT_TOKEN=... uvicorn app:app --port 8000

The page must be reachable over public https (a tunnel such as cloudflared
or ngrok is fine for trying it out). In MAX, also set APP_URL as the mini
app address in the bot's settings on business.max.ru.
"""

from __future__ import annotations

import asyncio
import html
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from matele import Button, Context, Dispatcher, Keyboard
from matele.max import MaxBot
from matele.telegram import TelegramBot
from matele.webapp import InitDataError

APP_URL = os.environ.get("APP_URL", "https://example.com/")

dp = Dispatcher()
bots: dict[str, MaxBot | TelegramBot] = {}
if token := os.environ.get("MAX_BOT_TOKEN"):
	bots["max"] = MaxBot(token)
if token := os.environ.get("TELEGRAM_BOT_TOKEN"):
	bots["telegram"] = TelegramBot(token, app_url=APP_URL)


@dp.command("start", description="Открыть магазин")
async def start(ctx: Context) -> None:
	keyboard = Keyboard([[Button("Открыть магазин", app="")], [Button("Сразу к кофе", app="coffee")]])
	await ctx.reply("Магазин открывается прямо в мессенджере:", keyboard)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
	if not bots:
		raise RuntimeError("Set MAX_BOT_TOKEN and/or TELEGRAM_BOT_TOKEN")
	polling = asyncio.create_task(dp.start_polling(*bots.values()))
	yield
	polling.cancel()
	for bot in bots.values():
		await bot.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def page() -> FileResponse:
	return FileResponse(Path(__file__).with_name("index.html"))


class Order(BaseModel):
	item: str


@app.post("/api/order")
async def order(order: Order, x_platform: str = Header(), x_init_data: str = Header()) -> dict[str, str]:
	bot = bots.get(x_platform)
	if bot is None:
		raise HTTPException(400, "unknown messenger")
	try:
		data = bot.verify_init_data(x_init_data)
	except InitDataError as exc:
		raise HTTPException(401, str(exc)) from None
	if data.user is None:
		raise HTTPException(400, "no user in initData")
	await bot.send_message(f"Заказ принят: <b>{html.escape(order.item)}</b>", user_id=data.user.id)
	return {"status": "ok"}
