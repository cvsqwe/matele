"""One bot, two messengers.

	MAX_BOT_TOKEN=... TELEGRAM_BOT_TOKEN=... python examples/two_messengers.py

Set either token or both: the bot runs on every messenger it has a token for.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from matele import (
	BotClient,
	Button,
	CallbackData,
	Context,
	Dispatcher,
	Keyboard,
	State,
	StatesGroup,
	callback,
	platform_title,
)
from matele.max import MaxBot
from matele.telegram import TelegramBot

dp = Dispatcher()


@callback("vote")
class Vote(CallbackData):
	choice: Literal["tea", "coffee"]


class Feedback(StatesGroup):
	text = State()


@dp.command("start")
async def start(ctx: Context) -> None:
	keyboard = Keyboard([[Button("🍵 Чай", Vote("tea").pack()), Button("☕ Кофе", Vote("coffee").pack())]])
	await ctx.reply(f"Привет, {ctx.user.name}! Вы пишете из {platform_title(ctx.platform)}.\nЧай или кофе?", keyboard)


@dp.callback(Vote)
async def vote(ctx: Context, payload: Vote) -> None:
	await ctx.answer("Голос принят")
	await ctx.edit(f"Вы выбрали: {'чай' if payload.choice == 'tea' else 'кофе'}. Оставить отзыв: /feedback")


@dp.command("feedback")
async def feedback(ctx: Context) -> None:
	ctx.state.set(Feedback.text)
	await ctx.reply("Напишите отзыв одним сообщением (или /cancel).")


@dp.command("cancel")
async def cancel(ctx: Context) -> None:
	ctx.state.clear()
	await ctx.reply("Отменено.")


@dp.message(state=Feedback.text)
async def got_feedback(ctx: Context) -> None:
	ctx.state.clear()
	await ctx.reply("Спасибо за отзыв!")


@dp.raw_callback()
async def outdated(ctx: Context) -> None:
	await ctx.answer("Эта кнопка устарела, нажмите /start", alert=True)


@dp.on_error
async def on_error(ctx: Context, exc: Exception) -> None:
	logging.exception("Handler failed", exc_info=exc)
	await ctx.answer("Что-то пошло не так", alert=True)


def main() -> None:
	logging.basicConfig(level=logging.INFO)
	bots: list[BotClient] = []
	if token := os.environ.get("MAX_BOT_TOKEN"):
		bots.append(MaxBot(token))
	if token := os.environ.get("TELEGRAM_BOT_TOKEN"):
		bots.append(TelegramBot(token))
	if not bots:
		raise SystemExit("Set MAX_BOT_TOKEN and/or TELEGRAM_BOT_TOKEN")
	dp.run_polling(*bots)


if __name__ == "__main__":
	main()
