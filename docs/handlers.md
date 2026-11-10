# Хендлеры и контекст

Хендлер это async-функция, которую matele вызывает на пришедшее событие. Она получает `Context`: в нём само событие, данные человека и методы для ответа.

## Виды хендлеров

```python
@dp.command("start", "help")       # /start или /help
async def start(ctx: Context): ...


@dp.message()                      # любое сообщение: текст, файл, фото
async def any_message(ctx: Context): ...


@dp.callback(SetStatus)            # нажатие кнопки с данными SetStatus
async def set_status(ctx: Context, payload: SetStatus): ...


@dp.raw_callback()                 # любое нажатие, в том числе на устаревшие кнопки
async def any_button(ctx: Context): ...


@dp.chat_status()                  # человек заблокировал бота или вернулся
async def status(ctx: Context): ...


@dp.member_update()                # кто-то вошёл в группу или вышел из неё
async def member(ctx: Context): ...
```

Про кнопки подробно рассказано в разделе [Кнопки](buttons.md), про события групп в разделе [Групповые чаты](groups.md).

## Команды

Команда это сообщение, которое начинается с `/`. Имя команды не зависит от регистра, `/Start` и `/start` попадут в один хендлер. Всё, что написано после имени, лежит в `ctx.args`:

```python
@dp.command("remind")
async def remind(ctx: Context):
    # "/remind купить хлеб" -> ctx.command == "remind", ctx.args == "купить хлеб"
    if not ctx.args:
        await ctx.reply("Напишите, о чём напомнить: /remind купить хлеб")
        return
    await ctx.reply(f"Напомню: {ctx.args}")
```

Если передать `description`, команда появится в меню команд обоих мессенджеров при запуске бота. Имя такой команды может состоять только из латинских букв в нижнем регистре, цифр и `_`, до 32 символов, а описание должно быть не длиннее 128 символов. Команды без описания работают, но в меню их не видно, так удобно прятать служебные команды.

## Контекст

Что есть в `ctx`:

| Атрибут | Что в нём |
|---|---|
| `ctx.user` | кто прислал событие: `id`, `name`, `username`, `platform` |
| `ctx.platform` | `"max"` или `"telegram"`, удобно сравнивать с `matele.MAX` и `matele.TELEGRAM` |
| `ctx.text` | текст сообщения, для остальных событий пустая строка |
| `ctx.message` | пришедшее сообщение или сообщение, на котором нажали кнопку |
| `ctx.command`, `ctx.args` | имя команды и текст после неё |
| `ctx.payload` | разобранные данные нажатой кнопки |
| `ctx.callback` | нажатие кнопки целиком, сырые данные в `ctx.callback.data` |
| `ctx.chat_type` | `matele.PRIVATE` или `matele.GROUP` |
| `ctx.chat_id` | id группы, в личном чате `None` |
| `ctx.state` | состояние разговора, см. [Состояния](states.md) |
| `ctx.bot` | бот, через который пришло событие |
| `ctx.data` | словарь для своих данных, которые middleware передаёт хендлеру |
| `ctx.match` | результат фильтра `Regex` |

И как ответить:

| Метод | Что делает |
|---|---|
| `await ctx.reply(text, keyboard)` | новое сообщение туда, откуда пришло событие |
| `await ctx.reply_file(path, kind="image")` | фото, видео, аудио или файл |
| `await ctx.send_private(text)` | сообщение человеку в личку, даже из группы |
| `await ctx.edit(text, keyboard)` | изменить сообщение, на котором нажали кнопку |
| `await ctx.show(text, keyboard)` | показать экран [меню в одном сообщении](menus.md) |
| `await ctx.answer(text)` | всплывающее уведомление в ответ на нажатие кнопки |
| `await ctx.delete()` | удалить пришедшее сообщение или сообщение с кнопкой |

Текст сообщений отправляется как HTML. Подробнее в разделе [Файлы и форматирование](files.md).

## Фильтры

Фильтр решает, подходит ли событие хендлеру. Фильтры передаются в декоратор после имён команд:

```python
from matele import ChatType, HasAttachment, OnPlatform, PRIVATE, Regex, TELEGRAM, Text


@dp.message(Text("привет", "hello", ignore_case=True))
async def hello(ctx: Context): ...


@dp.message(Regex(r"^заказ (\d+)$"))
async def order(ctx: Context):
    order_id = int(ctx.match.group(1))
    ...


@dp.message(HasAttachment("image"))
async def photo(ctx: Context): ...


@dp.command("stats", ChatType(PRIVATE))
async def stats(ctx: Context): ...


@dp.command("stars", OnPlatform(TELEGRAM))
async def stars(ctx: Context): ...
```

Встроенные фильтры:

- `Text(*variants, ignore_case=False)`: текст сообщения совпадает с одним из вариантов, пробелы по краям не важны.
- `Regex(pattern)`: текст сообщения (или данные кнопки) подходит под регулярное выражение, совпадение кладётся в `ctx.match`.
- `HasAttachment(*types)`: в сообщении есть вложение, если нужно, определённого типа (`"image"`, `"video"`, `"audio"`, `"file"`).
- `OnPlatform(*platforms)`: событие пришло из указанного мессенджера.
- `ChatType(*types)`: событие произошло в личном чате или в группе.

Свой фильтр это любая функция, которая получает `ctx` и возвращает `bool`. Она может быть обычной или async. Чтобы комбинировать фильтры через `&`, `|` и `~`, оберните функцию в `as_filter`:

```python
from matele import as_filter

ADMINS = {123456}
is_admin = as_filter(lambda ctx: ctx.user.id in ADMINS)


async def has_subscription(ctx: Context) -> bool:
    return await db.has_subscription(ctx.platform, ctx.user.id)


@dp.command("ban", is_admin)
async def ban(ctx: Context): ...


@dp.command("premium", is_admin | has_subscription)
async def premium(ctx: Context): ...
```

Фильтры одного хендлера проверяются по порядку и останавливаются на первом отказе. Поэтому дешёвые проверки лучше ставить раньше запросов к базе.

!!! note "id в разных мессенджерах"
    У одного и того же человека в MAX и Telegram разные id, и они могут совпасть с id другого человека в другом мессенджере. Храните пару `(ctx.platform, ctx.user.id)`, а не один id.

## Порядок хендлеров

Для каждого события вызывается не больше одного хендлера: первый, у которого подошли команда, состояние и фильтры. Порядок такой:

1. команды;
2. хендлеры, привязанные к состоянию (`state=...`);
3. все остальные.

Внутри каждой группы хендлеры проверяются в порядке регистрации. Благодаря этому `/cancel` срабатывает посреди любого сценария, а хендлер ввода для конкретного шага не перехватывается общим `@dp.message()`, даже если тот зарегистрирован раньше. Хендлеры нужно регистрировать до запуска бота, после первого события список закрывается.

## Роутеры

Когда хендлеров становится много, их удобно разложить по файлам. Каждый файл создаёт свой `Router`, а главный файл собирает их:

```python
# orders.py
from matele import Context, Router

router = Router("orders")


@router.command("orders", description="Мои заказы")
async def orders(ctx: Context): ...
```

```python
# bot.py
from matele import Dispatcher

import orders
import support

dp = Dispatcher()
dp.include(orders.router, support.router)
```

У `Router` те же декораторы, что у `Dispatcher`, и роутер тоже может подключать другие роутеры. Каждый роутер можно подключить только в одно место.

Три группы из раздела выше действуют для всех роутеров сразу: команда из любого роутера проверяется раньше, чем общий хендлер из любого другого. Внутри группы сначала идут хендлеры самого `Dispatcher`, потом роутеры в порядке подключения, а вложенные роутеры сразу после своего родителя.

## Middleware

Middleware это функция, которая оборачивает обработку каждого события. Она получает `ctx` и `call_next`. Код до `call_next()` выполняется перед хендлером, код после него выполняется, когда хендлер закончил. Если не вызвать `call_next()`, событие дальше не пойдёт.

```python
import time


@dp.middleware
async def timing(ctx: Context, call_next):
    started = time.monotonic()
    await call_next()
    logging.info("%s обработано за %.0f мс", ctx.platform, (time.monotonic() - started) * 1000)


@dp.middleware
async def banned(ctx: Context, call_next):
    if await db.is_banned(ctx.platform, ctx.user.id):
        return
    await call_next()
```

Middleware работает для всех событий, включая те, для которых не нашлось хендлера. Порядок вызова совпадает с порядком регистрации.

### Свои данные в контексте

Если middleware загружает что-то для хендлеров, например аккаунт из базы, сделайте свой класс контекста. Тогда у хендлеров будут подсказки типов:

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
```

Для разовых данных хватит словаря `ctx.data`.

## Ошибки

Исключение в хендлере или middleware не останавливает бота. Без своего обработчика оно просто пишется в лог. Обработчик ошибок получает контекст и исключение:

```python
@dp.on_error
async def on_error(ctx: Context, exc: Exception):
    logging.exception("Ошибка в хендлере", exc_info=exc)
    await ctx.answer("Что-то пошло не так", alert=True)
```

Даже если хендлер упал, состояние разговора сохранится, а нажатая кнопка перестанет крутиться: matele сам отвечает на нажатие, если хендлер этого не сделал.

Ошибки мессенджеров приходят как `BotApiError` с полями `status_code`, `code`, `message` и `platform`. Если человек заблокировал бота, это `BotForbiddenError`. Функция `is_unreachable(exc)` отвечает, значит ли ошибка, что человеку сейчас писать бесполезно.

## Человек заблокировал бота

```python
@dp.chat_status()
async def status(ctx: Context):
    if ctx.chat_status.reachable:
        await db.mark_active(ctx.platform, ctx.user.id)
    else:
        await db.mark_blocked(ctx.platform, ctx.user.id)
```

Событие приходит, когда человек заблокировал бота или снова его запустил. Написать ему в ответ на блокировку уже нельзя, а вот отметить у себя, чтобы не слать рассылки, стоит.
