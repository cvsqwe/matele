# Первый бот

На этой странице мы установим matele, получим токены и запустим бота, который отвечает в MAX и Telegram.

## Установка

Нужен Python 3.11 или новее. Создайте виртуальное окружение и поставьте matele:

```bash
python -m venv .venv
source .venv/bin/activate      # в Windows: .venv\Scripts\activate
pip install git+https://github.com/cvsqwe/matele
```

Дополнительные зависимости ставятся отдельно, только если они нужны:

| Что нужно | Как поставить |
|---|---|
| вебхуки | `pip install "matele[webhook] @ git+https://github.com/cvsqwe/matele"` |
| хранение состояний в Redis | `pip install "matele[redis] @ git+https://github.com/cvsqwe/matele"` |

SQLite и всё остальное работает без дополнительных пакетов.

## Токены

Токен это пароль бота: с ним любой может писать от имени бота. Не публикуйте его в репозитории, храните в переменных окружения или в файле, который не попадает в git.

В Telegram бота создаёт [@BotFather](https://t.me/BotFather): отправьте ему `/newbot`, придумайте имя и адрес, и он пришлёт токен.

В MAX боты создаются на платформе для бизнеса [business.max.ru](https://business.max.ru), там же выдаётся токен. Документация MAX для разработчиков лежит на [dev.max.ru](https://dev.max.ru).

Для одного мессенджера достаточно одного токена. Код бота от этого не меняется.

## Код

Создайте файл `bot.py`:

```python
import logging
import os

from matele import BotClient, Context, Dispatcher, escape
from matele.max import MaxBot
from matele.telegram import TelegramBot

dp = Dispatcher()


@dp.command("start", description="Начать")
async def start(ctx: Context):
    await ctx.reply(f"Привет, {escape(ctx.user.name)}! Напишите что-нибудь, и я повторю.")


@dp.message()
async def echo(ctx: Context):
    if ctx.text:
        await ctx.reply(escape(ctx.text))
    else:
        await ctx.reply("Я понимаю только текст")


def main():
    logging.basicConfig(level=logging.INFO)
    bots: list[BotClient] = []
    if token := os.environ.get("MAX_BOT_TOKEN"):
        bots.append(MaxBot(token))
    if token := os.environ.get("TELEGRAM_BOT_TOKEN"):
        bots.append(TelegramBot(token))
    dp.run_polling(*bots)


if __name__ == "__main__":
    main()
```

Что здесь происходит:

- `Dispatcher` хранит хендлеры и решает, какой из них вызвать для пришедшего события.
- `@dp.command("start")` ловит сообщение `/start`. Описание попадает в меню команд мессенджера.
- `@dp.message()` ловит все остальные сообщения. Команды проверяются раньше, поэтому `/start` сюда не попадёт.
- `ctx` содержит всё о событии: кто написал (`ctx.user`), что написал (`ctx.text`), из какого мессенджера (`ctx.platform`). Через него же бот отвечает.
- `run_polling` подключается к мессенджерам и забирает новые события, пока вы не остановите бота через Ctrl+C или сигналом SIGTERM (так сервис останавливают Docker и systemd). При остановке уже начатые хендлеры получают до 10 секунд, чтобы доработать (`shutdown_timeout`).
- Если процесс убить посреди работы хендлера, это событие по умолчанию теряется. `dp.run_polling(*bots, at_least_once=True)` меняет это: мессенджер пришлёт необработанные события снова после перезапуска. Тогда хендлеры должны спокойно переносить повтор события, а один медленный хендлер задерживает получение следующих событий этого мессенджера.

## Запуск

```bash
export MAX_BOT_TOKEN=...
export TELEGRAM_BOT_TOKEN=...
python bot.py
```

В PowerShell переменные задаются так: `$env:TELEGRAM_BOT_TOKEN = "..."`.

В логе появится по строке на каждый мессенджер:

```text
INFO:matele.polling.max:MAX: polling as @my_bot (id 123)
INFO:matele.polling.telegram:Telegram: polling as @my_bot (id 456)
```

Откройте бота в мессенджере и отправьте `/start`.

Если один мессенджер недоступен или токен неверный, в логе будут предупреждения и повторные попытки, а бот в другом мессенджере продолжит работать как обычно.

## Ссылки на запуск

Ссылка вида `https://t.me/my_bot?start=promo` открывает бота и сразу передаёт ему значение. В MAX такая ссылка выглядит как `https://max.ru/my_bot?start=promo`. В обоих мессенджерах значение приходит одинаково, как аргумент команды `/start`:

```python
@dp.command("start")
async def start(ctx: Context):
    if ctx.args == "promo":
        await ctx.reply("Вы пришли по промо-ссылке")
    else:
        await ctx.reply("Привет!")
```

Ссылку для текущего мессенджера строит сам бот: `ctx.bot.deep_link(ctx.bot.me.username, "promo")`.

## Что дальше

- [Хендлеры и контекст](handlers.md): команды, фильтры, роутеры, middleware и обработка ошибок.
- [Кнопки](buttons.md): клавиатуры и типизированные данные кнопок.
- [Состояния](states.md): сценарии из нескольких шагов, например анкеты.
