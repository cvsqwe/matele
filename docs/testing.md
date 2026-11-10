# Тестирование

`FakeBot` это ненастоящий мессенджер для тестов. Он создаёт входящие события и записывает всё, что бот отправил, изменил, удалил или на что ответил. Сеть и токены не нужны.

Примеры ниже написаны для pytest с плагином pytest-asyncio в режиме `asyncio_mode = "auto"`.

## Сообщения и команды

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

`dp.feed(event)` обрабатывает одно событие так же, как при работе бота: с middleware, фильтрами, состояниями и обработчиками ошибок. Возвращает `True`, если нашёлся хендлер.

Что записывает `FakeBot`:

| Поле | Что в нём |
|---|---|
| `bot.sent` | отправленные сообщения и файлы: `text`, `keyboard`, `chat_id`, `user_id`, `document`, `kind`, `reply_to`, `message` |
| `bot.texts` | только тексты отправленных сообщений |
| `bot.edits` | изменения сообщений: `message_id`, `text`, `keyboard` |
| `bot.deleted` | id удалённых сообщений |
| `bot.answers` | ответы на нажатия: `text`, `alert` |
| `bot.typing` | чаты, где показывался статус «печатает…» |
| `bot.commands` | меню команд, если его публиковали |

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

`press()` принимает объект данных кнопки или сырую строку. Чтобы проверить реакцию на устаревшую кнопку, передайте строку, которую бот уже не разбирает: `bot.press("old:format")`.

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

Если тесты используют один и тот же `dp`, состояние одного теста может остаться для следующего. Используйте разные `user_id` или создавайте `Dispatcher` для каждого теста.

## Мессенджер

По умолчанию `FakeBot` изображает MAX. Чтобы проверить ветки кода для Telegram, передайте платформу:

```python
from matele import TELEGRAM

bot = FakeBot(platform=TELEGRAM)
```

## Ошибки мессенджера

`FakeBot` ведёт себя как настоящий мессенджер в одном важном случае: изменить или удалить можно только сообщение, которое существует. На попытку изменить удалённое сообщение будет `BotApiError`. Другие ошибки можно изобразить, подменив метод:

```python
from matele import BotForbiddenError


async def test_blocked_user():
    bot = FakeBot()

    async def refuse(*args, **kwargs):
        raise BotForbiddenError(403, "chat.denied", "blocked", platform=bot.platform)

    bot.send_message = refuse
    await dp.feed(bot.message("/start"))
```
