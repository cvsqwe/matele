# Рассылки

`Hub` отправляет сообщения людям, которые пользуются ботом в одном или обоих мессенджерах. Он нужен для уведомлений и рассылок, которые бот отправляет сам, а не в ответ на сообщение.

```python
from matele.hub import Hub
from matele.max import MaxBot
from matele.telegram import TelegramBot

max_bot, telegram_bot = MaxBot(MAX_TOKEN), TelegramBot(TG_TOKEN)
hub = Hub(max_bot, telegram_bot)
```

`Hub` работает где угодно: в хендлере, в планировщике, в веб-админке. Запущенный `Dispatcher` ему не нужен. Если бот уже работает через `run_polling`, передайте в `Hub` те же объекты ботов.

## Одному человеку

Человек описывается словарём «мессенджер: id». Если он есть в обоих мессенджерах, сообщение придёт в оба:

```python
deliveries = await hub.send({"max": 123, "telegram": 456}, "Заказ готов")
for d in deliveries:
    if not d.ok:
        logging.warning("%s: не доставлено: %s", d.platform, d.error)
```

`send` не выбрасывает исключений. Результат по каждому мессенджеру лежит в `Delivery`: `platform`, `user_id`, `message` при успехе и `error` при ошибке. Сбой в одном мессенджере не мешает доставке во второй.

Где взять id: сохраняйте `ctx.platform` и `ctx.user.id`, когда человек пишет боту. Связать аккаунты одного человека в двух мессенджерах можно, например, через [ссылку на запуск](first-bot.md#ссылки-на-запуск) с одноразовым кодом.

## Многим людям

```python
import sqlite3

conn = sqlite3.connect("bot.db")


def subscribers():
    rows = conn.execute("SELECT max_id, telegram_id FROM subscribers WHERE NOT blocked")
    for max_id, telegram_id in rows:
        accounts = {}
        if max_id:
            accounts["max"] = max_id
        if telegram_id:
            accounts["telegram"] = telegram_id
        yield accounts


report = await hub.broadcast(subscribers(), "Мы открылись!")
print(report.people, report.reached, report.delivered, report.failed)
```

Получателей можно передать списком или генератором. Генератор читается постепенно, по мере отправки, так что большой список не придётся целиком держать в памяти.

В отчёте `BroadcastReport`:

- `people`: скольким людям пытались отправить;
- `reached`: сколько получили сообщение хотя бы в одном мессенджере;
- `delivered` и `failed`: успешные и неудачные отправки по отдельным мессенджерам;
- `unreachable`: список `(мессенджер, id)` тех, кто заблокировал бота или ни разу его не запускал;
- `deliveries`: все `Delivery` подряд.

## Скорость и сбои

Мессенджеры ограничивают, сколько сообщений бот может отправить в секунду, примерно 30 и в MAX, и в Telegram. `Hub` по умолчанию отправляет не больше 25 сообщений в секунду в каждый мессенджер. Скорость можно задать для всех мессенджеров или для каждого отдельно:

```python
hub = Hub(max_bot, telegram_bot, rate={"max": 20, "telegram": 25})
```

Если через одного бота отправляют несколько процессов, разделите лимит между ними, чтобы в сумме не превысить ограничение.

Если мессенджер всё же ответил «слишком часто», отправка повторится после паузы, которую он назвал. Если мессенджер ненадолго пропал из сети, `Hub` подождёт и повторит. Если он недоступен долго, отправки в него сразу заканчиваются ошибкой и не задерживают рассылку во второй мессенджер.

`concurrency` (по умолчанию 10) ограничивает, скольким людям сообщение отправляется одновременно. Общую скорость задаёт `rate`, а не `concurrency`.

## Те, кто заблокировал бота

Писать человеку, который заблокировал бота, бесполезно. Чтобы сразу отмечать таких у себя, передайте `on_unreachable`:

```python
async def mark_blocked(platform: str, user_id: int, exc: Exception) -> None:
    await db.mark_blocked(platform, user_id)


hub = Hub(max_bot, telegram_bot, on_unreachable=mark_blocked)
```

Узнать о блокировке можно и раньше, из хендлера `@dp.chat_status()`, см. [Хендлеры и контекст](handlers.md#человек-заблокировал-бота).

## Кнопки

У `send` и `broadcast` есть параметр `keyboard`, как у `ctx.reply`:

```python
await hub.broadcast(people, "Новая акция", Keyboard([[Button("Подробнее", url="https://example.com/sale")]]))
```
