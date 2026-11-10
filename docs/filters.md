# Фильтры

Фильтр решает, подходит ли событие хендлеру. Без фильтров `@dp.message()` получает все сообщения подряд. С фильтрами можно сказать точнее: только текст «привет», только фото, только в личном чате, только от администраторов.

Фильтры передаются в декоратор. У команд они идут после имён:

```python
from matele import ChatType, Context, HasAttachment, PRIVATE, Text


@dp.message(Text("привет", "hello", ignore_case=True))
async def hello(ctx: Context): ...


@dp.message(HasAttachment("image"))
async def photo(ctx: Context): ...


@dp.command("stats", ChatType(PRIVATE))
async def stats(ctx: Context): ...
```

Если фильтров несколько, должны пройти все.

## Встроенные фильтры

### Text

Текст сообщения совпадает с одним из вариантов целиком. Пробелы по краям не учитываются.

```python
@dp.message(Text("да", "yes", "ок", ignore_case=True))
async def confirmed(ctx: Context): ...
```

С `ignore_case=True` регистр не важен: подойдут и «Да», и «ДА». `Text` не ищет подстроку: сообщение «да, конечно» под фильтр `Text("да")` не попадёт. Для таких случаев есть `Regex`.

### Regex

Текст сообщения подходит под регулярное выражение. Результат поиска кладётся в `ctx.match`, оттуда удобно доставать группы:

```python
from matele import Regex


@dp.message(Regex(r"^заказ (\d+)$"))
async def order(ctx: Context):
    order_id = int(ctx.match.group(1))
    await ctx.reply(f"Ищу заказ {order_id}")
```

Выражение ищется в любом месте текста, как `re.search`. Чтобы требовать совпадения целиком, ставьте `^` и `$`. Вторым аргументом можно передать флаги, например `Regex("привет", re.IGNORECASE)`, или сразу готовый `re.compile(...)`.

Для нажатия кнопки `Regex` проверяет не текст, а сырые данные кнопки. Это бывает нужно в `@dp.raw_callback()`.

### HasAttachment

В сообщении есть вложение. Без аргументов подходит любое, с аргументами только указанных типов:

```python
@dp.message(HasAttachment())                    # любой файл
async def any_file(ctx: Context): ...


@dp.message(HasAttachment("image", "video"))    # фото или видео
async def media(ctx: Context): ...
```

Типы вложений: `"image"`, `"video"`, `"audio"`, `"file"`. Telegram дополнительно различает `"voice"`, `"animation"` и `"video_note"`. Как работать с самими файлами, написано в разделе [Файлы и форматирование](files.md#входящие-файлы).

### OnPlatform

Событие пришло из указанного мессенджера. Нужен, когда команда имеет смысл только в одном из них:

```python
from matele import OnPlatform, TELEGRAM


@dp.command("stars", OnPlatform(TELEGRAM))
async def stars(ctx: Context): ...
```

Если поведение отличается совсем немного, проще сравнить `ctx.platform` внутри хендлера.

### ChatType

Событие произошло в личном чате (`PRIVATE`) или в группе (`GROUP`):

```python
from matele import GROUP, ChatType


@dp.command("settings", ChatType(PRIVATE))
async def settings(ctx: Context): ...


@dp.command("rules", ChatType(GROUP))
async def rules(ctx: Context): ...
```

## Свои фильтры

Фильтр это любая функция, которая получает `ctx` и возвращает `bool`. Она может быть обычной или async:

```python
ADMINS = {("telegram", 123456), ("max", 987654)}


def is_admin(ctx: Context) -> bool:
    return (ctx.platform, ctx.user.id) in ADMINS


async def has_subscription(ctx: Context) -> bool:
    return await db.has_subscription(ctx.platform, ctx.user.id)


@dp.command("ban", is_admin)
async def ban(ctx: Context): ...


@dp.command("premium", has_subscription)
async def premium(ctx: Context): ...
```

Обратите внимание, что администраторы заданы парами «мессенджер и id». У одного человека в MAX и Telegram разные id, а один и тот же id в разных мессенджерах может принадлежать разным людям.

Если фильтру нужны параметры или своё состояние, унаследуйте его от `Filter` и опишите метод `check`:

```python
from matele import Filter


class MinLength(Filter):
    def __init__(self, length: int):
        self.length = length

    def check(self, ctx: Context) -> bool:
        return len(ctx.text) >= self.length


@dp.message(MinLength(20), state=Feedback.text)
async def got_feedback(ctx: Context): ...
```

Метод `check` тоже может быть async.

## Комбинирование

Фильтры можно соединять операторами `&` (и), `|` (или) и `~` (не). Встроенные фильтры и наследники `Filter` умеют это сразу, а обычную функцию нужно сначала обернуть в `as_filter`:

```python
from matele import as_filter

is_admin = as_filter(lambda ctx: (ctx.platform, ctx.user.id) in ADMINS)
subscribed = as_filter(has_subscription)


@dp.command("premium", is_admin | subscribed)
async def premium(ctx: Context): ...


@dp.message(Text("привет", ignore_case=True) & ~is_admin)
async def hello_stranger(ctx: Context): ...
```

Несколько фильтров через запятую работают как `&`. Отдельный `&` нужен, когда его надо совместить с `|` или `~`.

## Порядок проверки и стоимость

Фильтры одного хендлера проверяются по порядку и останавливаются на первом отказе. То же верно для `&`: если левая часть не прошла, правая не вызывается. Поэтому дешёвые проверки ставьте раньше запросов к базе:

```python
@dp.command("report", ChatType(PRIVATE), has_subscription)    # сначала тип чата, потом база
async def report(ctx: Context): ...
```

Помните, что фильтры вызываются для каждого события, которое дошло до хендлера по команде и состоянию. Если проверка требует запроса к базе и нужна многим хендлерам, выгоднее один раз загрузить данные в [middleware](routers.md#middleware) и положить в контекст, а в фильтрах только читать их:

```python
@dp.middleware
async def load_account(ctx: Context, call_next):
    ctx.data["account"] = await accounts.find(ctx.platform, ctx.user.id)
    await call_next()


def registered(ctx: Context) -> bool:
    return ctx.data["account"] is not None
```

Если фильтр упал с исключением, оно обрабатывается так же, как исключение в хендлере: уходит в обработчики ошибок или в лог, а событие остаётся без хендлера.

## Фильтр или проверка в хендлере

Фильтр выбирает хендлер. Если событие не прошло фильтр, диспетчер пробует следующий хендлер, и человек может вообще не получить ответа. Это хорошо, когда на одно и то же событие есть разные хендлеры: для администраторов один, для остальных другой.

Если же нужно отказать и объяснить почему, делайте проверку внутри хендлера:

```python
@dp.command("ban")
async def ban(ctx: Context):
    if not is_admin(ctx):
        await ctx.reply("Эта команда только для администраторов")
        return
    ...
```
