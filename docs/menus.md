# Меню в одном сообщении

Обычно бот на каждое нажатие отвечает новым сообщением, и через пару минут чат превращается в ленту из одинаковых клавиатур. `ctx.show()` работает иначе: у каждого человека есть одно сообщение-меню, и все экраны показываются в нём.

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

## Как это работает

- Нажатие кнопки меняет сообщение, на котором кнопка была нажата.
- Если человек что-то написал (команду, ответ на вопрос, файл), его сообщение после обработки удаляется, а меню обновляется. Если меню прямо над его сообщением, оно меняется на месте. Если меню далеко вверху, внизу появляется новое, а старое удаляется. Человек всегда видит ответ сразу под тем, что написал.
- `toast` показывает короткое уведомление в ответ на нажатие.

Telegram нумерует сообщения в чате, поэтому там matele знает, стоит ли меню прямо над сообщением человека. MAX номеров не сообщает, и там на каждое написанное сообщение меню отправляется заново внизу, а старое удаляется.

Если мессенджер не даёт удалить старое меню, matele убирает из него кнопки, чтобы нельзя было нажать устаревшие.

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

## Что ещё нужно знать

Id сообщения-меню хранится в состоянии разговора, в `ctx.state.meta`. Он переживает `ctx.state.clear()`, а с SQLite или Redis и перезапуск бота.

Уведомления, которые не являются экранами меню (например, «Ваш заказ доставлен»), лучше отправлять обычным `ctx.reply()`. Иначе следующее нажатие в меню заменит их.

В группе все видят меню всех участников и могут нажимать их кнопки. Проверяйте в хендлере, кому что можно, или оставьте меню только для личных чатов с помощью фильтра `ChatType(PRIVATE)`.
