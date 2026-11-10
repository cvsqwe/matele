# Состояния

Состояние помогает вести разговор из нескольких шагов: бот спросил имя, следующее сообщение человека это имя, а не новая команда. `ctx.state` хранит, на каком шаге человек сейчас, и данные, которые он уже ввёл.

## Анкета из трёх шагов

```python
from matele import Context, State, StatesGroup


class Signup(StatesGroup):
    name = State()
    age = State()
    city = State()


@dp.command("signup", description="Заполнить анкету")
async def signup(ctx: Context):
    ctx.state.set(Signup.name)
    await ctx.reply("Как вас зовут?")


@dp.command("cancel", description="Отменить")
async def cancel(ctx: Context):
    ctx.state.clear()
    await ctx.reply("Отменено")


@dp.message(state=Signup.name)
async def got_name(ctx: Context):
    ctx.state.update(name=ctx.text)
    ctx.state.set(Signup.age)
    await ctx.reply("Сколько вам лет?")


@dp.message(state=Signup.age)
async def got_age(ctx: Context):
    if not ctx.text.isdigit():
        await ctx.reply("Напишите возраст числом")
        return
    ctx.state.update(age=int(ctx.text))
    ctx.state.set(Signup.city)
    await ctx.reply("Из какого вы города?")


@dp.message(state=Signup.city)
async def got_city(ctx: Context):
    name, age = ctx.state.get("name"), ctx.state.get("age")
    ctx.state.clear()
    await ctx.reply(f"Готово: {name}, {age}, {ctx.text}")
```

`/cancel` работает на любом шаге: команды проверяются раньше хендлеров состояний (см. [порядок хендлеров](handlers.md#порядок-хендлеров)).

## Что умеет ctx.state

| Вызов | Что делает |
|---|---|
| `ctx.state.set(Signup.age)` | перейти в состояние |
| `ctx.state.set(None)` | выйти из состояния, данные останутся |
| `ctx.state.current` | имя текущего состояния или `None` |
| `ctx.state.is_in(Signup)` | человек на одном из шагов анкеты |
| `ctx.state.update(age=30)` | сохранить данные |
| `ctx.state.get("age")` | прочитать данные |
| `ctx.state.data` | все данные словарём |
| `ctx.state.clear()` | закончить разговор: ни состояния, ни данных |

Всё это синхронные вызовы. matele загружает состояние перед хендлером и сохраняет после, только если оно изменилось. Сохраняется оно и тогда, когда хендлер упал с ошибкой.

## Какие хендлеры к чему привязаны

Параметр `state` есть у `message`, `command`, `callback` и `raw_callback`:

| Параметр | Когда срабатывает хендлер |
|---|---|
| `state=Signup.name` | только на шаге `name` |
| `state=Signup` | на любом шаге анкеты |
| `state=(Signup.age, Signup.city)` | на одном из перечисленных шагов |
| `state=None` | только вне всех состояний |
| не указан | всегда |

Хендлер, привязанный к состоянию, проверяется раньше хендлера без привязки, так что общий `@dp.message()` не перехватит ответ на вопрос анкеты.

## Имена состояний

Состояние хранится по имени, по умолчанию это `"Signup:name"`. Если переименовать класс или атрибут, люди, которые были на этом шаге, окажутся в состоянии, которого больше нет, и их сообщения пойдут в общие хендлеры. Чтобы имя не зависело от кода, задайте его явно:

```python
class Signup(StatesGroup):
    name = State("signup:name")
    age = State("signup:age")
```

Состояние вне `StatesGroup` всегда требует имени: `State("ask_name")`.

## Где хранятся состояния

По умолчанию состояния лежат в памяти процесса и теряются при перезапуске. Для настоящего бота подключите хранилище:

```python
from matele.fsm import RedisStorage, SQLiteStorage

# один процесс бота
dp = Dispatcher(storage=SQLiteStorage("bot.db"))

# несколько процессов или серверов
dp = Dispatcher(storage=RedisStorage.from_url("redis://localhost:6379/0"))
```

SQLite работает на стандартной библиотеке Python. Запросы выполняются в отдельном потоке и не блокируют бота. Для Redis нужен пакет `matele[redis]`. Если у разговоров должен быть срок жизни, передайте `ttl` в секундах: `RedisStorage.from_url(url, ttl=7 * 86400)`. Разговор, который не менялся неделю, удалится сам.

`run_polling` и вебхуки закрывают хранилище при остановке бота.

Данные в `ctx.state` должны сериализоваться в JSON: строки, числа, списки, небольшие словари. Объекты из базы лучше не хранить, храните их id.

### Своё хранилище

Хранилище это любой класс с четырьмя async-методами: `get(key)`, `set(key, record)`, `delete(key)` и `close()`. `key` это `StorageKey(platform, user_id, chat_id)`, а `record` это `StateRecord` с полями `state`, `data` и `meta`. Протокол описан в `matele.fsm.Storage`.

## Чьё это состояние

Состояние хранится отдельно для каждого человека в каждом мессенджере. В группе оно отдельное для человека в этой группе: анкета, начатая в одной группе, не мешает в другой и в личном чате.

Одного человека matele обрабатывает последовательно: пока не закончился хендлер его предыдущего сообщения, следующее ждёт. Сообщения разных людей обрабатываются параллельно.
