# Справочник

Короткое описание всех публичных классов и параметров. Подробные объяснения и примеры есть в разделах руководства, ссылки на них стоят у каждого раздела.

## Dispatcher

```python
from matele import Dispatcher

dp = Dispatcher(storage=None, registry=None, context_class=Context)
```

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `storage` | `MemoryStorage()` | где хранятся состояния разговоров, см. [Состояния](states.md#где-хранятся-состояния) |
| `registry` | общий реестр | реестр классов данных кнопок. Свой нужен в тестах и плагинах |
| `context_class` | `Context` | класс контекста, который получают хендлеры, см. [Свои данные для хендлеров](routers.md#свои-данные-для-хендлеров) |

Декораторы, общие с `Router`:

| Декоратор | На что срабатывает |
|---|---|
| `@dp.command(*names, *filters, state=..., description=None)` | команды `/name` |
| `@dp.message(*filters, state=...)` | любые сообщения |
| `@dp.callback(DataClass, *filters, state=...)` | нажатие кнопки с данными этого класса, хендлер получает `(ctx, payload)` |
| `@dp.raw_callback(*filters, state=...)` | любое нажатие кнопки, в том числе устаревшей |
| `@dp.chat_status(*filters)` | человек заблокировал бота или вернулся |
| `@dp.member_update(*filters)` | вход и выход участников группы |

Только у `Dispatcher`:

| Метод | Что делает |
|---|---|
| `@dp.middleware` | регистрирует middleware `async def mw(ctx, call_next)` |
| `@dp.on_error` | регистрирует обработчик ошибок `async def handler(ctx, exc)` |
| `dp.include(*routers)` | подключает роутеры |
| `dp.run_polling(*bots, ...)` | запускает polling и блокирует программу до остановки |
| `await dp.start_polling(*bots, ...)` | то же, но как async-задача, без закрытия ботов в конце |
| `await dp.feed(event)` | обрабатывает одно событие, возвращает `True`, если нашёлся хендлер |
| `dp.menu_commands()` | список `(имя, описание)` для меню команд |
| `await dp.publish_commands(*bots)` | публикует меню команд. Polling и вебхуки делают это сами |

Параметры `run_polling` и `start_polling`:

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `timeout` | `30` | сколько секунд один запрос long polling ждёт новых событий |
| `shutdown_timeout` | `10.0` | сколько секунд при остановке ждать хендлеры, которые ещё работают |
| `concurrency` | `100` | сколько событий одного мессенджера обрабатывается одновременно |
| `at_least_once` | `False` | подтверждать события только после обработки, см. [Чтобы события не терялись](deploy.md#чтобы-события-не-терялись) |

## Router

```python
from matele import Router

router = Router("orders")
```

Те же декораторы, что у `Dispatcher`, и `router.include(*routers)`. Подробнее в разделе [Роутеры и middleware](routers.md).

## Context

Атрибуты и методы описаны в разделе [Хендлеры и контекст](handlers.md#контекст). Кратко:

| Что | Где |
|---|---|
| кто и откуда | `user`, `platform`, `chat_type`, `chat_id`, `bot` |
| событие | `event`, `text`, `message`, `command`, `args`, `callback`, `payload`, `chat_status`, `member_update`, `match` |
| данные | `state`, `data` |
| ответ | `reply()`, `reply_file()`, `send_private()`, `edit()`, `show()`, `answer()`, `delete()`, `typing()` |

## Фильтры

| Фильтр | Что проверяет |
|---|---|
| `Text(*variants, ignore_case=False)` | текст совпадает с одним из вариантов |
| `Regex(pattern, flags=0)` | текст подходит под выражение, результат в `ctx.match` |
| `HasAttachment(*types)` | в сообщении есть вложение |
| `OnPlatform(*platforms)` | событие из указанного мессенджера |
| `ChatType(*types)` | личный чат или группа |
| `as_filter(func)` | превращает функцию в фильтр, который можно комбинировать через `&`, `\|`, `~` |
| `Filter` | базовый класс для своих фильтров с методом `check(ctx)` |

Подробнее в разделе [Фильтры](filters.md).

## Кнопки

| Класс или функция | Что это |
|---|---|
| `Keyboard(rows)` | клавиатура: список рядов, ряд это список кнопок |
| `Button(text, data)` | кнопка с данными |
| `Button(text, url=...)` | кнопка-ссылка |
| `Button(text, app=...)` | кнопка мини-приложения |
| `@callback(prefix)` | превращает класс в данные кнопки |
| `CallbackData` | базовый класс данных кнопки, у объектов есть метод `pack()` |
| `action(prefix)` | готовый объект данных без полей |
| `parse(data)` | разбирает строку данных в объект, бросает `CallbackError` |
| `escape(value, quote=False)` | экранирует текст для HTML-сообщения |

Подробнее в разделе [Кнопки](buttons.md).

## Состояния

| Класс | Что это |
|---|---|
| `StatesGroup` | набор шагов одного сценария |
| `State(name=None)` | один шаг. Вне `StatesGroup` имя обязательно |
| `ANY_STATE` | значение `state=` по умолчанию: в любом состоянии |
| `MemoryStorage()` | хранилище в памяти процесса |
| `SQLiteStorage(path)` | хранилище в файле SQLite, из `matele.fsm` |
| `RedisStorage.from_url(url, prefix="matele", ttl=None, lock_ttl=60.0)` | хранилище в Redis, из `matele.fsm` |
| `StorageKey(platform, user_id, chat_id)` | ключ разговора в хранилище |
| `StateRecord(state, data, meta)` | запись разговора в хранилище |

Подробнее в разделе [Состояния](states.md).

## MaxBot

```python
from matele.max import MaxBot

max_bot = MaxBot(token, api=None, callback_secret=None)
```

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `api` | создаётся сам | свой клиент `MaxApi`, например с другим лимитом скачивания |
| `base_url` | адрес MAX API | другой адрес API |
| `callback_secret` | `None` | секрет подписи данных кнопок, см. [Подпись данных](buttons.md#подпись-данных) |
| `platform` | `"max"` | имя мессенджера в событиях. Менять не нужно |

## TelegramBot

```python
from matele.telegram import TelegramBot

telegram = TelegramBot(token, proxy=None, app_url=None, callback_secret=None)
```

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `proxy` | `None` | прокси для запросов к Telegram |
| `api_url` | `https://api.telegram.org` | свой сервер Bot API |
| `app_url` | `None` | адрес мини-приложения, нужен для `Button(app=...)` |
| `aliases` | в памяти | где хранить ключи длинных данных кнопок, см. [Длина данных](buttons.md#длина-данных) |
| `replace_webhook` | `False` | разрешить polling удалять вебхук, см. [Переход обратно на polling](webhooks.md#переход-обратно-на-polling) |
| `callback_secret` | `None` | секрет подписи данных кнопок |
| `api` | создаётся сам | свой клиент `TelegramApi` |
| `platform` | `"telegram"` | имя мессенджера в событиях. Менять не нужно |

## Методы ботов

Эти методы одинаковы у `MaxBot`, `TelegramBot` и `FakeBot`. Получатель задаётся ровно одним из `user_id=` (личный чат) и `chat_id=` (группа).

| Метод | Что делает |
|---|---|
| `send_message(text, user_id=, chat_id=, keyboard=None, reply_to=None)` | отправляет сообщение, возвращает `Message` |
| `send_file(source, kind="file", filename=None, caption=None, keyboard=None, ...)` | отправляет файл |
| `send_file_by_token(token, file_type="file", caption=None, ...)` | пересылает полученный файл без скачивания |
| `edit_message(message_id, text, keyboard=None)` | меняет сообщение. `text=None` меняет только кнопки |
| `delete_message(message_id)` | удаляет сообщение |
| `answer_callback(callback_id, text=None, alert=False)` | отвечает на нажатие кнопки |
| `download_file(token, file_type=, filename=, url=None)` | скачивает файл, возвращает `(содержимое, имя)` |
| `send_typing(chat_id)` | показывает «печатает…» |
| `set_commands(commands)` | заменяет меню команд |
| `deep_link(username, payload)` | ссылка на запуск бота со значением |
| `verify_init_data(init_data, max_age=3600)` | проверяет данные мини-приложения, только у `MaxBot` и `TelegramBot` |
| `get_me()` | узнаёт, кто этот бот. После вызова доступен `bot.me` |
| `close()` | закрывает соединения |

## Webhook

```python
from matele.webhook import Webhook

webhook = Webhook(dp, base_url=None, secret=None)
webhook.add(bot, "/path")
```

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `base_url` | `None` | публичный адрес сервера. Без него адреса не регистрируются в мессенджерах |
| `secret` | случайный | секрет, который мессенджеры присылают с каждым запросом |
| `max_pending` | `1000` | сколько событий может ждать обработки, прежде чем запросы получат 503 |
| `at_least_once` | `False` | отвечать мессенджеру только после обработки события |
| `delete_on_shutdown` | `False` | удалять вебхуки при остановке |
| `close_bots` | `True` | закрывать ботов при остановке |

Методы: `run(host, port, ...)`, `startup()`, `shutdown(timeout=10.0)`, `handle(path, headers, body)`. Подробнее в разделе [Вебхуки](webhooks.md).

## Hub

```python
from matele.hub import Hub

hub = Hub(*bots, rate=25.0, on_unreachable=None)
```

| Метод | Что делает |
|---|---|
| `await hub.send(accounts, text, keyboard=None)` | отправляет одному человеку во все его мессенджеры, возвращает список `Delivery` |
| `await hub.broadcast(people, text, keyboard=None, concurrency=10, on_sent=None)` | рассылка, возвращает `BroadcastReport` |

Подробнее в разделе [Рассылки](broadcasts.md).

## Ошибки

| Исключение или функция | Что значит |
|---|---|
| `BotApiError` | мессенджер отказал. Поля `status_code`, `code`, `message`, `platform` |
| `BotForbiddenError` | человеку нельзя написать: заблокировал бота или не запускал его |
| `BotUnavailableError` | мессенджер недоступен |
| `is_unreachable(exc)` | значит ли ошибка, что человеку сейчас писать бесполезно |
| `CallbackError` | строку данных кнопки не удалось разобрать |
| `InitDataError` | данные мини-приложения не прошли проверку, из `matele.webapp` |

Подробнее в разделе [Ошибки](errors.md).

## Константы

| Константа | Значение |
|---|---|
| `MAX`, `TELEGRAM` | `"max"`, `"telegram"`: значения `ctx.platform` |
| `PRIVATE`, `GROUP` | `"private"`, `"group"`: значения `ctx.chat_type` |
