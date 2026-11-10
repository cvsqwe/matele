# Роутеры и middleware

Пока в боте десяток хендлеров, им хватает одного файла. Дальше становится тесно: хендлеры заказов, поддержки и настроек перемешиваются, а в каждый второй приходится копировать одну и ту же проверку. Роутеры решают первую проблему, middleware вторую.

## Роутеры

`Router` это место, где регистрируются хендлеры одной части бота. У него те же декораторы, что у `Dispatcher`. Каждый файл создаёт свой роутер, а главный файл собирает их вместе:

```python
# orders.py
from matele import Context, Router

router = Router("orders")


@router.command("orders", description="Мои заказы")
async def orders(ctx: Context): ...


@router.message(state=Order.address)
async def got_address(ctx: Context): ...
```

```python
# bot.py
from matele import Dispatcher

import orders
import support

dp = Dispatcher()
dp.include(orders.router, support.router)
```

Имя роутера (`"orders"`) нужно только для сообщений об ошибках и отладки.

Сам `Dispatcher` тоже роутер, корневой. Хендлеры можно регистрировать и на нём, и на подключённых роутерах одновременно.

Роутер может подключать другие роутеры, так что большую часть бота можно разбить ещё раз:

```python
# shop/__init__.py
from matele import Router

from shop import cart, catalog

router = Router("shop")
router.include(catalog.router, cart.router)
```

Каждый роутер подключается только в одно место. Попытка подключить его второй раз закончится ошибкой `ValueError`.

### Порядок хендлеров между роутерами

Правило из раздела [Порядок хендлеров](handlers.md#порядок-хендлеров) действует для всех роутеров сразу, а не внутри каждого. Сначала проверяются команды из всех роутеров, потом хендлеры состояний из всех роутеров, потом остальные. Команда из любого роутера проверяется раньше, чем общий `@dp.message()` из любого другого.

Внутри каждой из трёх групп порядок такой: сначала хендлеры самого `Dispatcher`, потом роутеры в порядке подключения, а вложенные роутеры сразу после своего родителя.

На практике это значит, что порядок подключения роутеров важен только для хендлеров одной группы, которые могут подойти одному и тому же событию. Например, если в двух роутерах есть общий `@router.message()` без фильтров, сработает тот, чей роутер подключён раньше.

### Когда регистрировать

Все хендлеры и роутеры должны быть на месте до запуска бота. С первым событием диспетчер закрывает список, и после этого `include()` и декораторы бросают `RuntimeError`. Так ошибка вроде «забыл подключить роутер до запуска» видна сразу, а не проявляется в том, что часть хендлеров молча не работает.

## Middleware

Middleware это функция, которая оборачивает обработку каждого события. Она получает `ctx` и `call_next`. Код до `await call_next()` выполняется перед хендлером, код после него выполняется, когда хендлер закончил:

```python
import logging
import time


@dp.middleware
async def timing(ctx: Context, call_next):
    started = time.monotonic()
    await call_next()
    elapsed = (time.monotonic() - started) * 1000
    logging.info("%s: событие обработано за %.0f мс", ctx.platform, elapsed)
```

Если не вызвать `call_next()`, событие дальше не пойдёт: ни следующие middleware, ни хендлер его не увидят. Так делают блокировки:

```python
@dp.middleware
async def banned(ctx: Context, call_next):
    if await db.is_banned(ctx.platform, ctx.user.id):
        return
    await call_next()
```

Middleware регистрируется только на `Dispatcher` и работает для всех событий всех роутеров, включая те, для которых не нашлось хендлера. Несколько middleware вызываются в порядке регистрации: первый зарегистрированный оказывается самым внешним.

```python
@dp.middleware
async def first(ctx: Context, call_next):
    print("1: до")
    await call_next()
    print("1: после")


@dp.middleware
async def second(ctx: Context, call_next):
    print("2: до")
    await call_next()
    print("2: после")


# для каждого события: "1: до", "2: до", хендлер, "2: после", "1: после"
```

К моменту вызова middleware состояние разговора уже загружено, так что `ctx.state` доступен. А вот `ctx.command`, `ctx.args` и `ctx.payload` заполняются позже, когда диспетчер выбирает хендлер, то есть внутри `call_next()`. До вызова `call_next()` в middleware они ещё пустые.

### Свои данные для хендлеров

Частая задача middleware: загрузить что-то один раз и отдать всем хендлерам. Для разовых данных хватит словаря `ctx.data`:

```python
@dp.middleware
async def load_account(ctx: Context, call_next):
    ctx.data["account"] = await accounts.find(ctx.platform, ctx.user.id)
    await call_next()


@dp.command("me")
async def me(ctx: Context):
    account = ctx.data["account"]
```

Минус словаря в том, что редактор не знает, что в нём лежит. Если данные нужны многим хендлерам, сделайте свой класс контекста. Тогда у хендлеров будут подсказки и проверка типов:

```python
from matele import Context, Dispatcher


class Ctx(Context):
    account: Account | None = None


dp = Dispatcher(context_class=Ctx)


@dp.middleware
async def load_account(ctx: Ctx, call_next):
    ctx.account = await accounts.find(ctx.platform, ctx.user.id)
    await call_next()


@dp.command("me")
async def me(ctx: Ctx):
    if ctx.account is None:
        await ctx.reply("Вы ещё не зарегистрированы")
        return
    await ctx.reply(f"Ваш баланс: {ctx.account.balance}")
```

В свой класс контекста удобно добавлять и методы, например `ctx.t("key")` для переводов или `ctx.require_admin()`.

### Что класть в middleware

Хорошо подходит то, что касается каждого события:

- загрузка аккаунта или настроек человека;
- логирование и замер времени;
- блокировка нежелательных пользователей;
- ограничение частоты сообщений от одного человека;
- открытие и закрытие транзакции базы данных вокруг хендлера.

Не стоит класть туда логику, которая нужна паре хендлеров. Для неё есть [фильтры](filters.md) или обычная функция, которую хендлер вызывает сам.

### Ошибки в middleware

Исключение, которое вылетело из хендлера, проходит через все middleware наружу. Его можно перехватить обычным `try`:

```python
@dp.middleware
async def transaction(ctx: Context, call_next):
    async with db.transaction() as tx:
        ctx.data["tx"] = tx
        await call_next()       # если хендлер упал, транзакция откатится
```

Если исключение бросила сама middleware, оно обрабатывается так же, как исключение хендлера: уходит в [обработчики ошибок](errors.md) или в лог.
