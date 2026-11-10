# Кнопки

Кнопки в matele это кнопки под сообщением (inline-клавиатура). Клавиатура состоит из рядов, ряд из кнопок:

```python
from matele import Button, Keyboard

keyboard = Keyboard([
    [Button("Да", "answer:yes"), Button("Нет", "answer:no")],
    [Button("Сайт", url="https://example.com")],
])
await ctx.reply("Продолжим?", keyboard)
```

Кнопка бывает трёх видов:

- `Button(text, data)`: при нажатии бот получает `data`;
- `Button(text, url=...)`: открывает ссылку;
- `Button(text, app=...)`: открывает мини-приложение бота, см. [Мини-приложения](mini-apps.md).

## Данные кнопок

Данные кнопки это строка. Можно разбирать её вручную, но удобнее описать данные классом. Декоратор `@callback` превращает класс в неизменяемый dataclass и запоминает его префикс:

```python
from typing import Literal

from matele import CallbackData, Context, callback


@callback("order:set")
class SetStatus(CallbackData):
    order_id: int
    status: Literal["paid", "shipped"]


keyboard = Keyboard([[
    Button("Оплачен", SetStatus(42, "paid").pack()),       # "order:set:42:paid"
    Button("Отправлен", SetStatus(42, "shipped").pack()),
]])


@dp.callback(SetStatus)
async def set_status(ctx: Context, payload: SetStatus):
    await orders.set_status(payload.order_id, payload.status)
    await ctx.edit(f"Заказ {payload.order_id}: {payload.status}")
```

`pack()` собирает строку, а при нажатии matele разбирает её обратно и вызывает хендлер, зарегистрированный для этого класса. В хендлер приходит готовый объект, где `order_id` уже `int`.

Поля могут быть типов `int`, `str`, `bool`, `datetime.date` или `Literal` из строк. Значение `str` с двоеточием допускается только в последнем поле, потому что двоеточие разделяет поля.

Префикс должен быть уникальным. Если два класса объявят один и тот же префикс, при импорте будет ошибка, так что две части бота никогда не перехватят кнопки друг друга.

### Кнопки без полей

Для кнопок, у которых нет данных, есть `action`:

```python
from matele import action

HOME = action("menu:home")

keyboard = Keyboard([[Button("На главную", HOME.pack())]])


@dp.callback(HOME)
async def home(ctx: Context, payload):
    await ctx.show("Главное меню", main_keyboard)
```

### Новые поля и старые кнопки

Кнопки, отправленные вчера, остаются в чатах и после обновления бота. Чтобы добавить поле, не сломав их, дайте ему значение по умолчанию и добавьте в конец класса:

```python
@callback("order:set")
class SetStatus(CallbackData):
    order_id: int
    status: Literal["paid", "shipped"]
    notify: bool = True
```

Поля в конце, равные значению по умолчанию, в строку не попадают. Старая строка `"order:set:42:paid"` разберётся с `notify=True`.

Если кнопку разобрать не удалось (например, класс удалили или поменяли тип поля), бот не упадёт. Такое нажатие придёт в `@dp.raw_callback()`, а `ctx.payload` будет `None`:

```python
@dp.raw_callback()
async def outdated(ctx: Context):
    await ctx.answer("Эта кнопка устарела, нажмите /start", alert=True)
```

Если такого хендлера нет, matele просто ответит на нажатие, чтобы кнопка перестала крутиться.

## Длина данных

Telegram ограничивает данные кнопки 64 байтами. Буква кириллицы занимает два байта, так что в данные лучше класть id, а не названия.

Если данные всё же длиннее, matele заменит их коротким ключом, а при нажатии вернёт исходное значение. Ключи хранятся в памяти и пропадают при перезапуске. Чтобы старые длинные кнопки работали и после перезапуска, храните ключи в том же файле SQLite, что и состояния:

```python
from matele.fsm import SQLiteStorage
from matele.telegram import CallbackAliases, TelegramBot

storage = SQLiteStorage("bot.db")
dp = Dispatcher(storage=storage)
telegram = TelegramBot(TG_TOKEN, aliases=CallbackAliases(storage.aliases()))
```

Ключи, которые бот не отправлял в кнопках 60 дней, удаляются. Своё хранилище ключей можно сделать по протоколу `matele.telegram.AliasStore`: методы `save`, `get` и `purge`.

## Ответ на нажатие

Пока бот не ответил на нажатие, кнопка в Telegram показывает часики. matele отвечает сам после хендлера, но ответ можно сделать полезным:

```python
await ctx.answer("Сохранено")                         # короткое уведомление
await ctx.answer("Недостаточно прав", alert=True)     # окно, которое нужно закрыть
```

Учитывается только первый вызов. В MAX нет окон с кнопкой «ОК», поэтому там `alert=True` показывает уведомление и дополнительно присылает текст человеку обычным сообщением.

## Изменить сообщение с кнопкой

`ctx.edit()` меняет текст и клавиатуру того сообщения, на котором нажали кнопку:

```python
@dp.callback(SetStatus)
async def set_status(ctx: Context, payload: SetStatus):
    await ctx.edit("Готово")              # кнопки исчезнут
    await ctx.edit("Готово", keyboard)    # кнопки заменятся
    await ctx.edit(None, keyboard)        # текст останется, заменятся только кнопки
```

Если клавиатуру не передать, она будет убрана.

## Ссылки в кнопках

Мессенджеры строже относятся к ссылкам в кнопках, чем к ссылкам в тексте. MAX отклоняет сообщение целиком, если ссылка ведёт на адрес, который он не считает публичным, например `localhost` или IP. Telegram принимает в кнопках только `http`, `https` и `tg`. matele не даёт такому сообщению пропасть: в MAX ссылка переносится в текст, а в Telegram кнопка остаётся, но ничего не открывает.

## Права на кнопки

Данные кнопки приходят от клиента мессенджера, и модифицированный клиент может прислать что угодно. Проверяйте права в хендлере, а не полагайтесь на то, что кнопку видел только нужный человек:

```python
@dp.callback(SetStatus)
async def set_status(ctx: Context, payload: SetStatus):
    order = await orders.get(payload.order_id)
    if order.owner != (ctx.platform, ctx.user.id):
        await ctx.answer("Это не ваш заказ", alert=True)
        return
    ...
```

В группах это особенно важно: кнопки под сообщением бота может нажать любой участник.
