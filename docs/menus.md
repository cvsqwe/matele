# Меню в одном сообщении

Обычно бот на каждое нажатие отвечает новым сообщением, и через пару минут чат превращается в ленту из одинаковых клавиатур. Старые кнопки остаются выше, человек нажимает не ту, и бот отвечает на вопрос трёхминутной давности.

`ctx.show()` работает иначе: у каждого человека есть одно сообщение-меню, и все экраны бота показываются в нём. Нажал кнопку, и сообщение поменялось на месте, как страница в приложении.

```python
from matele import Button, CallbackData, Context, Keyboard, action, callback

HOME = action("menu:home")
ORDERS = action("menu:orders")


@callback("menu:order")
class OpenOrder(CallbackData):
    order_id: int


def main_keyboard() -> Keyboard:
    return Keyboard([[Button("Мои заказы", ORDERS.pack())]])


@dp.command("menu", description="Главное меню")
async def menu(ctx: Context):
    await ctx.show("Главное меню", main_keyboard())


@dp.callback(HOME)
async def home(ctx: Context, payload):
    await ctx.show("Главное меню", main_keyboard())


@dp.callback(ORDERS)
async def orders(ctx: Context, payload):
    rows = [[Button(f"Заказ {o.id}", OpenOrder(o.id).pack())] for o in await db.orders(ctx.user.id)]
    rows.append([Button("Назад", HOME.pack())])
    await ctx.show("Ваши заказы", Keyboard(rows), toast="Загружено")
```

Во всех трёх хендлерах вызов один и тот же: `ctx.show(текст, клавиатура)`. Хендлер не думает о том, менять сообщение или отправлять новое, это решает matele.

## Как это работает

Что делает `ctx.show()`, зависит от того, какое событие обрабатывается.

Человек нажал кнопку. Меняется то сообщение, на котором была нажата кнопка. Если мессенджер не даёт его изменить, например оно слишком старое, отправляется новое.

Человек что-то написал: команду, ответ на вопрос, файл. Его сообщение после обработки удаляется, чтобы чат не заполнялся вводом, а дальше есть два случая:

- меню стоит прямо над его сообщением: оно меняется на месте;
- меню далеко вверху: внизу появляется новое, а старое удаляется.

В обоих случаях человек видит ответ сразу под тем, что написал, а меню в чате остаётся одно.

`toast` показывает короткое уведомление в ответ на нажатие, как `ctx.answer()`.

`ctx.show()` возвращает отправленное сообщение, если отправил новое, и `None`, если изменил существующее.

### Отличия мессенджеров

Telegram нумерует сообщения в чате, поэтому там matele знает, стоит ли меню прямо над сообщением человека. MAX номеров не сообщает, и там на каждое написанное сообщение меню отправляется заново внизу, а старое удаляется. На нажатия кнопок это не влияет: в обоих мессенджерах сообщение меняется на месте.

Если мессенджер не даёт удалить старое меню, matele убирает из него кнопки, чтобы нельзя было нажать устаревшие.

Удалить сообщение человека бот может не всегда. В личном чате Telegram разрешает это в течение 48 часов, в группе боту нужны права администратора. Если удалить не вышло, меню всё равно обновится, а сообщение человека просто останется в чате.

## Вопросы с проверкой ввода

Меню хорошо сочетается с [состояниями](states.md): экран задаёт вопрос, человек отвечает, его ответ исчезает, а меню показывает следующий шаг.

```python
from matele import State

ASK_EMAIL = State("profile:email")


@dp.callback(action("profile:edit_email"))
async def edit_email(ctx: Context, payload):
    ctx.state.set(ASK_EMAIL)
    await ctx.show("Напишите новый email")


@dp.message(state=ASK_EMAIL)
async def got_email(ctx: Context):
    if "@" not in ctx.text:
        await ctx.show("Это не похоже на email, попробуйте ещё раз", keep_input=True)
        return
    await db.set_email(ctx.user.id, ctx.text)
    ctx.state.clear()
    await ctx.show("Email сохранён", main_keyboard())
```

С `keep_input=True` сообщение человека остаётся в чате. Это полезно, когда ввод не прошёл проверку: длинный текст можно скопировать и исправить, а не набирать заново.

## Экран как функция

Когда экранов становится много, удобно описывать каждый функцией, которая возвращает текст и клавиатуру. Тогда один и тот же экран можно показать из разных хендлеров:

```python
async def order_screen(order_id: int) -> tuple[str, Keyboard]:
    order = await db.order(order_id)
    text = f"<b>Заказ {order.id}</b>\nСтатус: {order.status}"
    keyboard = Keyboard([
        [Button("Отменить", CancelOrder(order.id).pack())],
        [Button("К списку", ORDERS.pack())],
    ])
    return text, keyboard


@dp.callback(OpenOrder)
async def open_order(ctx: Context, payload: OpenOrder):
    await ctx.show(*await order_screen(payload.order_id))


@dp.callback(CancelOrder)
async def cancel_order(ctx: Context, payload: CancelOrder):
    await db.cancel(payload.order_id)
    await ctx.show(*await order_screen(payload.order_id), toast="Заказ отменён")
```

## Что ещё нужно знать

Id сообщения-меню хранится в состоянии разговора, в `ctx.state.meta`. Он переживает `ctx.state.clear()`, а с хранилищем SQLite или Redis и перезапуск бота. С хранилищем в памяти бот после перезапуска забудет, где у кого меню, и отправит новое.

Уведомления, которые не являются экранами меню, например «Ваш заказ доставлен», отправляйте обычным `ctx.reply()` или `bot.send_message()`. Они остаются отдельными сообщениями, и следующее нажатие в меню их не заменит.

В группе все видят меню всех участников и могут нажимать их кнопки. Проверяйте в хендлере, кому что можно, или оставьте меню только для личных чатов с помощью фильтра `ChatType(PRIVATE)`.

`ctx.show()` и `ctx.edit()` можно смешивать. `ctx.edit()` всегда меняет сообщение с нажатой кнопкой и годится для разовых сообщений с кнопками, например «Подтвердить заказ?». `ctx.show()` нужен, когда бот ведёт человека по экранам.
