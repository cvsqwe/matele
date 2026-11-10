# Тестирование

Проверять бота руками долго: запустить, открыть мессенджер, пройти сценарий, повторить после каждой правки. Тесты делают то же самое за секунду и без мессенджера.

Для этого в matele есть `FakeBot`: ненастоящий мессенджер. Он создаёт входящие события и записывает всё, что бот отправил, изменил, удалил или на что ответил. Сеть и токены не нужны.

Примеры ниже написаны для pytest с плагином pytest-asyncio:

```bash
pip install pytest pytest-asyncio
```

В `pyproject.toml` включите режим, в котором async-тесты запускаются без дополнительных пометок:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## Как устроен тест

Тест состоит из трёх действий: создать `FakeBot`, скормить диспетчеру событие, проверить, что бот сделал.

```python
from matele.testing import FakeBot

from bot import dp


async def test_start():
    bot = FakeBot()
    await dp.feed(bot.message("/start"))
    assert bot.sent[-1].text == "Привет! Напишите что-нибудь, и я повторю."


async def test_echo():
    bot = FakeBot()
    await dp.feed(bot.message("как дела?"))
    assert bot.texts == ["как дела?"]
```

`bot.message("/start")` создаёт событие: сообщение с таким текстом от человека с id 1.

`dp.feed(event)` обрабатывает одно событие так же, как при работе настоящего бота: с middleware, фильтрами, состояниями и обработчиками ошибок. Он возвращает `True`, если нашёлся хендлер, и никогда не бросает исключений: ошибки хендлеров уходят в обработчики ошибок или в лог.

Чтобы тест мог импортировать `dp`, запуск бота в `bot.py` должен быть под `if __name__ == "__main__":`. Иначе импорт запустит polling.

## Что записывает FakeBot

| Поле | Что в нём |
|---|---|
| `bot.sent` | отправленные сообщения и файлы |
| `bot.texts` | только тексты отправленных сообщений, по порядку |
| `bot.edits` | изменения сообщений: `message_id`, `text`, `keyboard` |
| `bot.deleted` | id удалённых сообщений |
| `bot.answers` | ответы на нажатия кнопок: `text`, `alert` |
| `bot.typing` | чаты, где показывался статус «печатает…» |
| `bot.commands` | меню команд, если его публиковали |

У каждой записи в `bot.sent` есть поля:

| Поле | Что в нём |
|---|---|
| `text` | текст сообщения или подпись к файлу |
| `keyboard` | клавиатура или `None` |
| `user_id` | кому отправлено, если это личный чат |
| `chat_id` | куда отправлено, если это группа |
| `message` | отправленное сообщение, его передают в `bot.press()` |
| `document` | для файла: пара из имени и содержимого |
| `kind` | для файла: `"image"`, `"video"`, `"audio"` или `"file"` |
| `reply_to` | id сообщения, на которое это ответ |

## Создание событий

| Вызов | Какое событие |
|---|---|
| `bot.message("текст")` | сообщение в личном чате |
| `bot.message("текст", user_id=5)` | сообщение от другого человека |
| `bot.message("текст", chat_id=-100)` | сообщение в группе |
| `bot.message(attachments=[...])` | сообщение с файлом |
| `bot.press(данные, message=...)` | нажатие кнопки |
| `bot.blocked(user_id=3)` | человек заблокировал бота |
| `bot.blocked(user_id=3, reachable=True)` | человек вернулся |
| `bot.member(-100, user_id=2)` | человек вошёл в группу |
| `bot.member(-100, user_id=2, joined=False)` | человек вышел из группы |
| `bot.member(-100, user_id=bot.bot_id)` | бота добавили в группу |

## Кнопки

```python
async def test_vote():
    bot = FakeBot()
    await dp.feed(bot.message("/start"))
    menu = bot.sent[-1]

    await dp.feed(bot.press(Vote("tea"), message=menu.message))

    assert bot.answers[-1].text == "Голос принят"
    assert bot.edits[-1].text.startswith("Вы выбрали: чай")
```

`press()` принимает объект данных кнопки или сырую строку. В `message` передают сообщение, под которым «нажата» кнопка: без него `ctx.edit()` в хендлере не сработает, потому что редактировать нечего.

Заодно стоит проверить, что нужная кнопка в клавиатуре вообще есть:

```python
async def test_start_has_buttons():
    bot = FakeBot()
    await dp.feed(bot.message("/start"))
    buttons = [b for row in bot.sent[-1].keyboard.rows for b in row]
    assert [b.data for b in buttons] == [Vote("tea").pack(), Vote("coffee").pack()]
```

Чтобы проверить реакцию на устаревшую кнопку, передайте строку, которую бот уже не разбирает: `bot.press("old:format")`.

## Сценарии с состояниями

Состояние сохраняется между вызовами `feed`, как в настоящем боте, поэтому сценарий проверяется последовательностью сообщений:

```python
async def test_signup():
    bot = FakeBot()
    for text in ["/signup", "Аня", "двадцать", "20", "Казань"]:
        await dp.feed(bot.message(text))
    assert bot.texts[-2] == "Из какого вы города?"
    assert bot.texts[-1] == "Готово: Аня, 20, Казань"
```

Здесь заодно проверено, что на «двадцать» бот переспросил, а не сломался.

### Чтобы тесты не мешали друг другу

Если тесты используют один и тот же `dp`, состояние одного теста останется для следующего: первый тест бросил анкету на середине, и во втором тесте сообщение человека с id 1 попадёт в неё. Есть два способа этого избежать.

Первый: в каждом тесте брать своего человека.

```python
async def test_cancel():
    bot = FakeBot()
    await dp.feed(bot.message("/signup", user_id=42))
    await dp.feed(bot.message("/cancel", user_id=42))
    assert bot.texts[-1] == "Отменено"
```

Второй, надёжнее: подменять хранилище на чистое перед каждым тестом.

```python
import pytest

from matele.fsm import MemoryStorage


@pytest.fixture(autouse=True)
def clean_state():
    dp.storage = MemoryStorage()
```

## Несколько человек и группы

```python
async def test_group():
    bot = FakeBot()
    await dp.feed(bot.message("/stats", user_id=1, chat_id=-100))
    assert bot.sent[-1].chat_id == -100

    await dp.feed(bot.member(-100, user_id=2))                  # человек 2 вошёл в группу
    await dp.feed(bot.member(-100, user_id=bot.bot_id))         # бота добавили в группу
    await dp.feed(bot.blocked(user_id=3))                       # человек 3 заблокировал бота
```

Сообщение в группу отличается от сообщения в личный чат тем, что в записи `bot.sent` заполнен `chat_id`, а не `user_id`. Так проверяют, что `ctx.send_private()` действительно ушёл в личку:

```python
async def test_code_goes_to_private_chat():
    bot = FakeBot()
    await dp.feed(bot.message("/mycode", user_id=7, chat_id=-100))
    private, public = bot.sent
    assert private.user_id == 7 and private.text.startswith("Ваш код")
    assert public.chat_id == -100
```

## Файлы

Сообщение с файлом собирается из `Attachment`, а отправленный ботом файл лежит в поле `document`:

```python
from matele import Attachment


async def test_export():
    bot = FakeBot()
    await dp.feed(bot.message("/export"))
    filename, content = bot.sent[-1].document
    assert filename == "orders.csv"
    assert content.startswith("id,сумма".encode())


async def test_incoming_photo():
    bot = FakeBot()
    photo = Attachment(type="image", token="photo-1", filename="cat.jpg")
    await dp.feed(bot.message(attachments=[photo]))
    assert bot.texts[-1] == "Красивое фото"
```

## Мессенджер

По умолчанию `FakeBot` изображает MAX. Чтобы проверить ветки кода для Telegram, передайте платформу:

```python
from matele import TELEGRAM

bot = FakeBot(platform=TELEGRAM)
```

Имя пользователя и id самого бота задаются параметрами `username` и `bot_id`.

## Ошибки мессенджера

`FakeBot` ведёт себя как настоящий мессенджер в одном важном случае: изменить или удалить можно только сообщение, которое существует. На попытку изменить удалённое сообщение будет `BotApiError`.

Другие ошибки можно изобразить, подменив метод. Так проверяют, что бот правильно ведёт себя, когда человек его заблокировал:

```python
from matele import BotForbiddenError


async def test_blocked_user():
    bot = FakeBot()

    async def refuse(*args, **kwargs):
        raise BotForbiddenError(403, "chat.denied", "blocked", platform=bot.platform)

    bot.send_message = refuse
    await dp.feed(bot.message("/start"))
```

## Что FakeBot не проверяет

`FakeBot` проверяет логику вашего бота, а не правила мессенджеров. Он примет сообщение с незакрытым тегом, кнопку с данными длиннее 64 байт и файл на десять гигабайт. Он не знает, что бот не может написать человеку первым.

Поэтому тесты не отменяют проверку руками в настоящих мессенджерах перед выпуском. Для неё удобно завести отдельного тестового бота с отдельным токеном.
