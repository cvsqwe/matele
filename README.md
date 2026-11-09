# matele

Фреймворк на Python для ботов, которые работают сразу в MAX и Telegram. Хендлеры пишутся один раз, а разницу между мессенджерами matele берёт на себя.

Проект на ранней стадии: API ещё может меняться, на PyPI пока не опубликован.

## Установка

```bash
pip install git+https://github.com/cvsqwe/matele
```

Нужен Python 3.11 или новее. Для вебхуков добавьте `[webhook]`, для хранения состояния в Redis `[redis]`:

```bash
pip install "matele[webhook,redis] @ git+https://github.com/cvsqwe/matele"
```

## Пример

Бот, который здоровается, спрашивает имя и запоминает его. Один и тот же код работает в обоих мессенджерах:

```python
from matele import Button, CallbackData, Context, Dispatcher, Keyboard, State, callback
from matele.max import MaxBot
from matele.telegram import TelegramBot

dp = Dispatcher()
ASK_NAME = State("ask_name")


@callback("hello")
class Hello(CallbackData):
    lang: str


@dp.command("start")
async def start(ctx: Context):
    keyboard = Keyboard([[Button("Русский", Hello("ru").pack()), Button("English", Hello("en").pack())]])
    await ctx.reply("Привет! На каком языке говорим?", keyboard)


@dp.callback(Hello)
async def chose_language(ctx: Context, payload: Hello):
    ctx.state.set(ASK_NAME)
    ctx.state.update(lang=payload.lang)
    await ctx.edit("Как тебя зовут?" if payload.lang == "ru" else "What's your name?")


@dp.message(state=ASK_NAME)
async def got_name(ctx: Context):
    lang = ctx.state.get("lang")
    ctx.state.clear()
    await ctx.reply(f"Приятно познакомиться, {ctx.text}!" if lang == "ru" else f"Nice to meet you, {ctx.text}!")


dp.run_polling(MaxBot(MAX_TOKEN), TelegramBot(TG_TOKEN))
```

Можно запустить только один из ботов, если второй мессенджер не нужен. Пример побольше лежит в [examples/two_messengers.py](examples/two_messengers.py).

## Кнопки

Данные кнопки описываются классом, а не строкой, которую потом приходится резать вручную. В хендлер приходит уже готовый объект с нужными типами:

```python
@callback("order:set")
class SetStatus(CallbackData):
    order_id: int
    status: Literal["paid", "shipped"]


@dp.callback(SetStatus)
async def set_status(ctx: Context, payload: SetStatus):
    await ctx.edit(f"Заказ {payload.order_id}: {payload.status}")
```

Telegram ограничивает данные кнопки 64 байтами. Если payload длиннее, matele подменяет его коротким ключом, а при нажатии возвращает исходное значение. Нажатия на кнопки из старых версий бота, которые уже не разбираются, приходят в `@dp.raw_callback()`, и бот не падает.

## Фильтры и порядок хендлеров

Фильтр это любая функция от контекста, обычная или async. Фильтры комбинируются через `&`, `|` и `~`:

```python
is_admin = as_filter(lambda ctx: ctx.user.id in ADMINS)

@dp.command("ban", is_admin)
async def ban(ctx: Context): ...
```

Встроенные фильтры: `Text`, `Regex`, `HasAttachment`, `OnPlatform`, `ChatType`.

Хендлер выбирается в предсказуемом порядке: сначала команды, потом хендлеры текущего состояния, потом все остальные. Поэтому `/cancel` срабатывает посреди любого сценария, и не нужно следить, в каком порядке что зарегистрировано.

## Состояния

`ctx.state` хранит, на каком шаге сценария находится человек, и данные, которые он успел ввести. По умолчанию всё лежит в памяти и теряется при перезапуске. Чтобы переживало перезапуск, подключите хранилище:

```python
from matele.fsm import SQLiteStorage, RedisStorage

dp = Dispatcher(storage=SQLiteStorage("bot.db"))
# или, если процессов несколько:
dp = Dispatcher(storage=RedisStorage.from_url("redis://localhost:6379/0"))
```

SQLite работает на стандартной библиотеке, ничего ставить не нужно.

## Группы

Бот работает и в личке, и в групповых чатах. `ctx.reply()` отвечает туда, откуда пришло сообщение, а `ctx.send_private()` пишет человеку в личку. Состояние хранится отдельно для каждого человека в каждом чате, так что форма, начатая в одной группе, не мешает в другой.

```python
@dp.member_update()
async def welcome(ctx: Context):
    update = ctx.member_update
    if update.joined and not update.is_me:
        await ctx.reply(f"Добро пожаловать, {update.member.name}!")
```

Команды вроде `/help@other_bot` бот пропускает: они адресованы не ему.

## Вебхуки

Вместо постоянного опроса мессенджеры могут сами присылать события на ваш сервер:

```python
from matele.webhook import Webhook

webhook = Webhook(dp, base_url="https://bot.example.com")
webhook.add(MaxBot(MAX_TOKEN), "/max")
webhook.add(TelegramBot(TG_TOKEN), "/telegram")
webhook.run(port=8080)
```

`Webhook` это обычное ASGI-приложение, его можно запустить и через `uvicorn app:webhook`. При старте он сам регистрирует адреса в мессенджерах со случайным секретом и проверяет этот секрет в каждом запросе. Нужен публичный HTTPS-адрес: MAX принимает только порт 443, Telegram ещё 80, 88 и 8443.

## Весь MAX Bot API

Помимо общего слоя есть типизированный клиент ко всем 33 методам MAX Bot API: чаты, участники, закрепы, загрузка файлов, вебхуки, комментарии.

```python
from matele.max import MaxApi

async with MaxApi(token) as api:
    photo = await api.upload(Path("cat.jpg"), type="image")
    sent = await api.send_message(chat_id=chat_id, text="<b>Кот</b>", format="html", attachments=[photo])
    await api.pin_message(chat_id, sent.body.mid)
```

Модели генерируются из [официальной схемы](https://github.com/max-messenger/api-schema), поэтому совпадают с документацией MAX. Если MAX добавит новое поле или новый тип события, бот не упадёт: неизвестное просто приходит как базовый объект с исходными данными в `.raw`. Сертификат Минцифры, которым подписан MAX API, уже встроен и используется только для запросов к MAX.

Из хендлера клиент доступен как `ctx.bot.api`. Для Telegram есть `TelegramApi.call()`, через него можно вызвать любой метод Bot API.

## Тесты

Ботов можно тестировать без сети и токенов:

```python
from matele.testing import FakeBot

bot = FakeBot()
await dp.feed(bot.message("/start"))
assert bot.sent[-1].text == "Привет! На каком языке говорим?"
```

## Что дальше

- одно сообщение-меню, которое редактируется вместо того, чтобы засыпать чат новыми
- отправка человеку сразу во все мессенджеры, где он есть
- документация и релиз на PyPI

## Разработка

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check src tests tools examples
.venv/bin/basedpyright
```

В коде отступы табами, настройки в `.editorconfig`.

## Лицензия

MIT
