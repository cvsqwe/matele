# API мессенджеров

Общий слой matele покрывает то, что есть в обоих мессенджерах. Всё остальное доступно через клиенты API конкретного мессенджера: закрепы, управление участниками, комментарии, стикеры, платежи и так далее.

## Внутри хендлера

У каждого бота есть свойство `api`. Чтобы редакторы и проверка типов знали, какой это мессенджер, сначала проверьте тип бота:

```python
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

`message_id` в matele это строка, которую нужно передавать обратно тому же мессенджеру. В MAX это обычный id сообщения. В Telegram это `"chat_id:message_id"`, потому что Telegram для изменения сообщения требует оба значения. `TelegramBot.split_id()` разбирает такую строку.

## MAX

`MaxApi` это типизированный клиент ко всем методам MAX Bot API: чаты, участники и администраторы, закрепы, сообщения, загрузка файлов, вебхуки, видео и комментарии. Каждый метод соответствует одному методу из [документации MAX](https://dev.max.ru/docs-api) и возвращает модели из `matele.max.models`.

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

Модели генерируются из [официальной схемы MAX](https://github.com/max-messenger/api-schema), поэтому имена полей совпадают с документацией. Если MAX добавит новое поле или новый тип события, бот не упадёт: неизвестный объект приходит как базовая модель, а исходные данные лежат в `.raw`.

Что ещё делает клиент:

- повторяет запросы при ответах 429, 502, 503 и 504 с учётом `Retry-After`;
- превращает ошибки MAX в `BotApiError`, а ответы 403 в `BotForbiddenError`;
- доверяет сертификату Минцифры, которым подписан MAX API. Сертификат встроен в пакет и используется только для запросов к MAX, остальные соединения программы он не затрагивает;
- не передаёт токен бота серверам загрузки и скачивания файлов.

## Telegram

Для Telegram matele не описывает каждый метод отдельно. `TelegramApi.call()` вызывает любой метод [Telegram Bot API](https://core.telegram.org/bots/api) по имени, параметры передаются как именованные аргументы:

```python
from matele.telegram import InputFile, TelegramApi

async with TelegramApi(token) as api:
    me = await api.call("getMe")
    await api.call("sendMessage", chat_id=42, text="<b>Привет</b>", parse_mode="HTML")
    await api.call("sendDocument", chat_id=42, document=InputFile(b"a,b\n1,2\n", "report.csv"))
```

Параметры со значением `None` не отправляются, словари и списки отправляются как JSON. Результат возвращается как обычные данные JSON, без моделей. Файлы передаются через `InputFile`.

Повторы при 429 и ошибках шлюза, ошибки `BotApiError` и `BotForbiddenError` работают так же, как в MAX. Токен бота не попадает в тексты ошибок и логи.

Если Telegram у вас доступен только через прокси, передайте его боту: `TelegramBot(token, proxy="http://proxy.example.com:3128")`. Для SOCKS-прокси дополнительно нужен пакет `httpx[socks]`. Для своего сервера Bot API есть параметр `api_url`.

## Когда мессенджер недоступен

Если до мессенджера не удалось достучаться два раза подряд, следующие запросы к нему 30 секунд сразу заканчиваются `BotUnavailableError`, а не ждут каждый свой таймаут. Считаются только сбои соединения: один медленный ответ или зависшая загрузка файла остальным запросам не мешают. Так недоступный мессенджер не задерживает работу с другим. Long polling продолжает пробовать подключиться, и как только мессенджер ответит, запросы снова пойдут как обычно.
