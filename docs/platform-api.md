# API мессенджеров

Общий слой matele покрывает то, что есть в обоих мессенджерах и работает одинаково. Но у каждого мессенджера есть и свои возможности: закреплённые сообщения, управление участниками, опросы, стикеры, платежи. Для них у каждого бота есть клиент API его мессенджера.

Писать через клиент API приходится реже, чем кажется. Сначала проверьте, нет ли нужного в общем слое: отправка, изменение и удаление сообщений, файлы, кнопки и группы там есть.

## Внутри хендлера

Клиент лежит в свойстве `api` бота. Какой мессенджер у события, заранее неизвестно, поэтому сначала проверьте тип бота. Заодно редактор и проверка типов поймут, какие методы доступны:

```python
from matele import GROUP, ChatType, Context
from matele.max import MaxBot
from matele.telegram import TelegramBot


@dp.command("pin", ChatType(GROUP))
async def pin(ctx: Context):
    sent = await ctx.reply("Важное объявление")
    if isinstance(ctx.bot, MaxBot):
        await ctx.bot.api.pin_message(ctx.chat_id, sent.message_id)
    elif isinstance(ctx.bot, TelegramBot):
        chat_id, message_id = TelegramBot.split_id(sent.message_id)
        await ctx.bot.api.call("pinChatMessage", chat_id=chat_id, message_id=message_id)
```

Если команда нужна только в одном мессенджере, вместо проверки типа можно поставить фильтр `OnPlatform`.

### Идентификаторы сообщений

`message_id` в matele это строка, которую нужно передавать обратно тому же мессенджеру. В MAX это обычный id сообщения, и его можно передавать в методы `MaxApi` как есть. В Telegram это `"chat_id:message_id"`, потому что Telegram для изменения сообщения требует оба значения. `TelegramBot.split_id()` разбирает такую строку на два числа, как в примере выше.

### Идентификаторы чатов

`ctx.chat_id` в личном чате равен `None`, а клиентам API нужен настоящий id. Он лежит в сообщении:

```python
chat_id = ctx.message.chat_id      # id чата и в группе, и в личном чате
```

В Telegram id личного чата совпадает с id человека. В MAX это разные числа: у диалога с человеком свой `chat_id`, а методы отправки `MaxApi` принимают и его, и `user_id` самого человека.

## MAX

`MaxApi` это типизированный клиент ко всем методам MAX Bot API. Каждый его метод соответствует одному методу из [документации MAX](https://dev.max.ru/docs-api) и возвращает модели из `matele.max.models`.

```python
from pathlib import Path

from matele.max import MaxApi

async with MaxApi(token) as api:
    me = await api.get_me()
    photo = await api.upload(Path("cat.jpg"), type="image")
    sent = await api.send_message(chat_id=chat_id, text="<b>Кот</b>", format="html", attachments=[photo])
    await api.pin_message(chat_id, sent.body.mid)
    members = await api.get_members(chat_id)
```

Что есть в клиенте:

| Раздел | Методы |
|---|---|
| бот | `get_me`, `set_my_commands` |
| чаты | `get_chat`, `edit_chat`, `leave_chat`, `send_action` |
| закреплённое сообщение | `get_pinned_message`, `pin_message`, `unpin_message` |
| участники | `get_members`, `add_members`, `remove_member`, `get_membership` |
| администраторы | `get_admins`, `add_admins`, `remove_admin` |
| сообщения | `send_message`, `edit_message`, `delete_message`, `get_message`, `get_messages` |
| кнопки | `answer_callback` |
| комментарии к публикациям | `get_comments`, `get_comment`, `send_comment`, `edit_comment`, `delete_comment` |
| файлы | `upload`, `get_upload_url`, `download`, `get_video` |
| события | `get_updates`, `subscribe`, `unsubscribe`, `get_subscriptions` |

Если нужного метода в клиенте ещё нет, например MAX добавил новый, его можно вызвать через `api.request()`: он принимает HTTP-метод, путь и параметры и возвращает разобранный JSON.

```python
data = await api.request("GET", "/chats", params={"count": 50})
```

### Модели

Модели генерируются из [официальной схемы MAX](https://github.com/max-messenger/api-schema), поэтому имена классов и полей совпадают с документацией MAX.

Клиент устойчив к изменениям на стороне MAX. Если мессенджер добавит новое поле или новый тип события, бот не упадёт: неизвестное поле игнорируется, а неизвестный тип объекта приходит как базовая модель. Исходные данные в обоих случаях лежат в свойстве `.raw`:

```python
update_data = some_update.raw      # словарь в том виде, в каком его прислал MAX
```

### Файлы

Отправка файла в MAX идёт в два шага: сначала файл загружается на сервер, потом прикрепляется к сообщению. `api.upload()` делает первый шаг и возвращает готовое вложение для параметра `attachments`. Сразу после загрузки файл может быть ещё не обработан, и MAX отклонит сообщение. Клиент в этом случае сам повторяет отправку несколько секунд.

Метод `bot.send_file()` из общего слоя делает оба шага за вас.

### Что ещё делает клиент

- повторяет запросы при ответах 429, 502, 503 и 504 с учётом заголовка `Retry-After`;
- превращает ошибки MAX в `BotApiError`, а отказы вида «этому человеку написать нельзя» в `BotForbiddenError`;
- доверяет сертификату Минцифры, которым подписан MAX API. Сертификат встроен в пакет и используется только для запросов к MAX, остальные соединения программы он не затрагивает;
- не передаёт токен бота серверам загрузки и скачивания файлов.

## Telegram

У Telegram Bot API больше сотни методов, и они часто меняются. matele не описывает каждый отдельно. `TelegramApi.call()` вызывает любой метод [Telegram Bot API](https://core.telegram.org/bots/api) по имени, а параметры передаются как именованные аргументы:

```python
from matele.telegram import InputFile, TelegramApi

async with TelegramApi(token) as api:
    me = await api.call("getMe")
    await api.call("sendMessage", chat_id=42, text="<b>Привет</b>", parse_mode="HTML")
    await api.call("sendDocument", chat_id=42, document=InputFile(b"a,b\n1,2\n", "report.csv"))
```

Имена методов и параметров берутся из документации Telegram как есть. Правила простые:

- параметры со значением `None` не отправляются;
- словари и списки отправляются как JSON, собирать строку самому не нужно;
- результат это поле `result` из ответа Telegram: обычные словари и списки, без моделей;
- файлы передаются через `InputFile`. В него можно положить байты, путь `Path` или открытый файл.

Пример посложнее, опрос:

```python
@dp.command("poll", OnPlatform(TELEGRAM))
async def poll(ctx: Context):
    assert isinstance(ctx.bot, TelegramBot)
    await ctx.bot.api.call(
        "sendPoll",
        chat_id=ctx.message.chat_id,
        question="Когда встречаемся?",
        options=[{"text": "В субботу"}, {"text": "В воскресенье"}],
        is_anonymous=False,
    )
```

Повторы при 429 и ошибках шлюза, ошибки `BotApiError` и `BotForbiddenError` работают так же, как в MAX. Токен бота не попадает в тексты ошибок и логи.

### События, которых нет в общем слое

matele просит у Telegram только те события, которые умеет превращать в общие: сообщения, нажатия кнопок и изменения статуса бота. Другие события Telegram (ответы на опросы, платежи, запросы в inline-режиме) matele пока не получает, и до хендлеров они не доходят. Отправлять опросы и счета через `api.call()` можно, а реакцию на них придётся отслеживать по обычным событиям: сообщениям и нажатиям кнопок.

### Прокси и свой сервер

Если Telegram у вас доступен только через прокси, передайте его боту:

```python
telegram = TelegramBot(TG_TOKEN, proxy="http://proxy.example.com:3128")
```

Для SOCKS-прокси дополнительно нужен пакет `httpx[socks]`. Для своего сервера Bot API есть параметр `api_url`.

## Свои настройки HTTP

Оба клиента работают через `httpx`. Если нужны свои таймауты, прокси или подмена сервера в тестах, клиенту можно передать готовый `httpx.AsyncClient`, а клиент передать боту:

```python
import httpx

from matele.max import MaxApi, MaxBot
from matele.max.api import ssl_context

api = MaxApi(MAX_TOKEN, http=httpx.AsyncClient(timeout=60, verify=ssl_context()))
max_bot = MaxBot(MAX_TOKEN, api=api)
```

Клиент, который вы создали сами, matele не закрывает: это ваша ответственность.

Для MAX есть одна тонкость. MAX API подписан сертификатом Минцифры, которого нет среди стандартных корневых сертификатов, и клиент, созданный matele, доверяет ему отдельно. Если создаёте `httpx.AsyncClient` для MAX сами, передайте ему этот набор сертификатов через `verify=ssl_context()`, как в примере выше, иначе запросы закончатся ошибкой TLS.

## Когда мессенджер недоступен

Если до мессенджера не удалось достучаться два раза подряд, следующие запросы к нему 30 секунд сразу заканчиваются `BotUnavailableError`, а не ждут каждый свой таймаут. Так недоступный мессенджер не задерживает работу с другим. Подробнее в разделе [Ошибки](errors.md#когда-мессенджер-недоступен).
