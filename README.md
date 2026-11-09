# matele

**Один бот — два мессенджера.** Асинхронный фреймворк на Python для ботов, которые одинаково работают в MAX и Telegram: обработчики пишутся один раз, транспорт берёт на себя различия платформ.

> ⚠️ Pre-alpha. Публичный API ещё меняется.

## Зачем

Многим организациям сейчас нужен бот и в Telegram, и в MAX. Писать и поддерживать два бота дорого, а у мессенджеров много мелких различий: формат id сообщений, лимит callback-данных в Telegram (64 байта), отсутствие модальных алертов в MAX, разные правила для кнопок-ссылок, отдельный корневой сертификат для MAX API. matele прячет их за общими типами.

## Как это выглядит

```python
from typing import Literal

from matele import Button, CallbackData, Context, Dispatcher, Keyboard, State, StatesGroup, as_filter, callback
from matele.max import MaxBot
from matele.telegram import TelegramBot

dp = Dispatcher()


@callback("order:set")
class SetStatus(CallbackData):  # payload кнопки — dataclass, а не строка
	order_id: int
	status: Literal["paid", "shipped"]


class Feedback(StatesGroup):
	text = State()


is_manager = as_filter(lambda ctx: ctx.user.id in MANAGERS)


@dp.command("start")
async def start(ctx: Context):
	await ctx.reply(f"Привет! Вы пишете из {ctx.platform}.")


@dp.command("order", is_manager)
async def order(ctx: Context):
	keyboard = Keyboard([[Button("✅ Оплачен", SetStatus(42, "paid").pack())]])
	await ctx.reply("Заказ №42", keyboard)


@dp.callback(SetStatus, is_manager)
async def set_status(ctx: Context, payload: SetStatus):
	await ctx.edit(f"Заказ №{payload.order_id}: {payload.status}")


@dp.command("feedback")
async def feedback(ctx: Context):
	ctx.state.set(Feedback.text)
	await ctx.reply("Напишите отзыв одним сообщением")


@dp.message(state=Feedback.text)
async def got_feedback(ctx: Context):
	ctx.state.clear()
	await ctx.reply("Спасибо!")


dp.run_polling(MaxBot(MAX_TOKEN), TelegramBot(TG_TOKEN))  # один бот — оба мессенджера
```

## Возможности

- **Общие типы** для обоих мессенджеров: `Message`, `CallbackQuery`, `Keyboard`, `Button`, `User`, `Attachment`.
- **Типизированные кнопки**: `@callback("prefix")` + `@dp.callback(Payload)` — хендлер получает уже разобранный объект. Кнопки от старых версий бота не ломают его: их ловит `@dp.raw_callback()`.
- **Роутеры** с предсказуемым порядком: команды → хендлеры текущего состояния → остальные. `/cancel` срабатывает посреди сценария, не нужно следить за порядком регистрации.
- **Фильтры** — любые функции (sync или async), комбинируются через `&`, `|`, `~`. Встроенные: `Text`, `Regex`, `HasAttachment`, `OnPlatform`.
- **Middleware** вокруг каждого события и **свой класс контекста** с типизированными полями.
- **FSM**: `StatesGroup`/`State`, данные сценария. Хранилища: `MemoryStorage`, `SQLiteStorage` (стандартная библиотека, переживает перезапуск), `RedisStorage` (`matele[redis]`, общее состояние для нескольких процессов) или своё через протокол `Storage`.
- **Групповые чаты**: `ctx.reply()` отвечает туда, откуда пришло сообщение, фильтр `ChatType(PRIVATE, GROUP)`, состояние диалога отдельно для каждого человека в каждом чате, события `@dp.member_update()` (бота добавили или удалили, участник вошёл или вышел). Команды вида `/help@другой_бот` игнорируются.
- **Надёжность**: кнопка всегда получает ответ (даже если хендлер упал), состояние сохраняется и после ошибки, события одного человека обрабатываются по порядку, а разные люди не ждут друг друга. Если один мессенджер недоступен, второй продолжает работать (`ConnectionGuard`).
- **Транспорты** `MaxBot` и `TelegramBot`: прячут различия платформ (deep-link `/start` в MAX приходит отдельным событием, лимит 64 байта на callback-данные в Telegram, отсутствие модальных алертов в MAX, кнопки-ссылки на localhost, битый HTML и т. д.).
- **Тестирование без сети**: `matele.testing.FakeBot`.

```python
from matele.testing import FakeBot

bot = FakeBot()
await dp.feed(bot.message("/start"))
assert bot.sent[-1].text.startswith("Привет")
```

## Вебхуки

Вместо long polling мессенджеры могут сами присылать события на ваш HTTPS-сервер:

```python
from matele.webhook import Webhook

webhook = Webhook(dp, base_url="https://bot.example.com")
webhook.add(MaxBot(MAX_TOKEN), "/max")
webhook.add(TelegramBot(TG_TOKEN), "/telegram")

webhook.run(port=8080)  # pip install matele[webhook], или: uvicorn app:webhook
```

`Webhook` — обычное ASGI-приложение без зависимостей: при старте регистрирует адреса в мессенджерах со случайным секретом и проверяет его в каждом запросе, сразу отвечает 200 и обрабатывает события в фоне, сохраняя порядок для каждого человека. Мессенджерам нужен публичный HTTPS-адрес: MAX принимает только порт 443, Telegram — 443, 80, 88 или 8443.

## Полный MAX Bot API

Кроме общего слоя, `matele` даёт типизированный клиент ко **всем 33 методам** MAX Bot API — чаты, участники, администраторы, закрепы, вебхуки, загрузка файлов, комментарии:

```python
from pathlib import Path

from matele.max import MaxApi

async with MaxApi(token) as api:
	chat = await api.get_chat(chat_id)
	photo = await api.upload(Path("cat.jpg"), type="image")
	sent = await api.send_message(chat_id=chat.chat_id, text="<b>Кот</b>", format="html", attachments=[photo])
	await api.pin_message(chat.chat_id, sent.body.mid)
```

Модели (`matele.max.models`, 120 классов) **генерируются из официальной OpenAPI-схемы** MAX (`tools/gen_max_models.py`), поэтому совпадают с документацией и обновляются одной командой. Разбор устойчив к изменениям API: новые поля игнорируются, неизвестный тип вложения или события не роняет бота (приходит базовый объект с `.raw`). Корневой сертификат Минцифры, которым подписан MAX API, встроен и используется только для запросов к MAX.

Из обработчика доступен через `ctx.bot.api`. Для Telegram есть `TelegramApi.call(method, **params)` — любой метод Bot API, включая загрузку файлов (`InputFile`).

## Дорожная карта

- [x] Ядро: типы, ошибки, callback-данные
- [x] Роутер, диспетчер, FSM, фильтры, middleware, long polling, FakeBot
- [x] Транспорты MAX и Telegram, полный MAX Bot API (модели из официальной схемы)
- [x] Групповые чаты, вебхуки, хранилища SQLite/Redis
- [ ] Однооконная навигация (panel), доставка сразу во все мессенджеры (hub)
- [ ] Документация, примеры, релиз 0.1 на PyPI

## Разработка

Нужен Python 3.11+.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/basedpyright
```

Отступы — табы (см. `.editorconfig`).

## Лицензия

MIT
