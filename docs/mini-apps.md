# Мини-приложения

Мини-приложение это веб-страница, которая открывается прямо в мессенджере по кнопке бота. На ней можно сделать то, что неудобно делать кнопками и сообщениями: каталог с картинками, корзину, форму с десятком полей, календарь.

Мини-приложение состоит из трёх частей, и matele помогает с двумя из них:

1. кнопка в сообщении бота, которая открывает страницу;
2. сама страница: обычные HTML и JavaScript, её вы пишете сами;
3. сервер, которому страница отправляет запросы. Ему нужно знать, кто именно открыл страницу, и matele это проверяет.

Готовый пример со страницей и сервером на FastAPI лежит в [examples/mini_app](https://github.com/cvsqwe/matele/tree/main/examples/mini_app). Его можно запустить и разобрать по частям.

## Кнопка

```python
from matele import Button, Keyboard

keyboard = Keyboard([
    [Button("Открыть магазин", app="")],
    [Button("Сразу к кофе", app="coffee")],
])
await ctx.reply("Магазин открывается прямо в мессенджере:", keyboard)
```

Значение `app` приложение получает как параметр запуска. По нему удобно сразу открыть нужную страницу: раздел каталога, конкретный заказ. Пустая строка значит «без параметра». В значении допускаются латинские буквы, цифры, `_` и `-`, не больше 512 символов.

### Где задаётся адрес страницы

У бота одно мини-приложение, и его адрес в кнопке не указывается. Мессенджеры узнают его по-разному:

- в MAX адрес мини-приложения указывается в настройках бота на [business.max.ru](https://business.max.ru);
- в Telegram его передают боту при создании: `TelegramBot(token, app_url="https://shop.example.com/")`.

Страница должна быть доступна по HTTPS с настоящим сертификатом. Для проверки на своём компьютере подойдёт туннель вроде cloudflared или ngrok.

В Telegram такие кнопки работают только в личных чатах. Если отправить её в группу, Telegram отклонит сообщение.

## Страница

На странице подключается скрипт мессенджера. Он даёт доступ к `initData`: строке с данными о человеке, который открыл приложение. Мессенджер подписывает эту строку токеном бота, поэтому подделать её нельзя.

Одна и та же страница может работать в обоих мессенджерах. Для этого подключают оба скрипта и смотрят, какой из них получил данные:

```html
<!-- Telegram -->
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<!-- MAX -->
<script src="https://st.max.ru/js/max-web-app.js"></script>

<script>
  const tg = window.Telegram && window.Telegram.WebApp;
  const inTelegram = Boolean(tg && tg.initData);
  const platform = inTelegram ? "telegram" : "max";
  const initData = inTelegram ? tg.initData : window.WebApp.initData;

  // значение app="..." у кнопки: Telegram передаёт его в адресе, MAX в initData
  const startParam = inTelegram
    ? new URLSearchParams(location.search).get("startapp")
    : window.WebApp.initDataUnsafe.start_param;

  async function order(item) {
    await fetch("/api/order", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Platform": platform,
        "X-Init-Data": initData,
      },
      body: JSON.stringify({ item }),
    });
  }
</script>
```

Страница отправляет серверу две вещи вместе с каждым запросом: из какого она мессенджера и строку `initData` целиком. Имена заголовков `X-Platform` и `X-Init-Data` это просто соглашение из примера, можно выбрать любые.

Полный код страницы для обоих мессенджеров есть в [index.html из примера](https://github.com/cvsqwe/matele/blob/main/examples/mini_app/index.html).

## Проверка на сервере

Главное правило: никогда не верьте id пользователя, который страница прислала сама. Запрос к вашему серверу может отправить кто угодно и с любым содержимым, не открывая мессенджер. Если сервер принимает `{"user_id": 123, "item": "кофе"}` и верит полю `user_id`, любой может сделать заказ от чужого имени.

Единственное, чему можно верить, это `initData` с проверенной подписью:

```python
from matele.webapp import InitDataError

try:
    data = bot.verify_init_data(request.headers["X-Init-Data"])
except InitDataError:
    return 401

await bot.send_message("Заказ принят", user_id=data.user.id)
```

`verify_init_data` есть и у `MaxBot`, и у `TelegramBot`. Она проверяет две вещи:

- подпись: что строку действительно создал мессенджер для этого бота и её никто не менял;
- срок: что данные не старше часа. Иначе перехваченную однажды строку можно было бы использовать вечно.

Срок меняется параметром `max_age` в секундах, а `max_age=None` отключает проверку срока. Если проверка не прошла по любой причине, функция бросает `InitDataError`.

### Что в результате

Функция возвращает `WebAppData`:

| Поле | Что в нём |
|---|---|
| `user` | кто открыл приложение: `User` с `id`, `name`, `username`. Может быть `None` |
| `start_param` | в MAX значение `app` у кнопки |
| `language` | язык интерфейса человека, например `"ru"` |
| `auth_date` | когда мессенджер подписал данные, время Unix |
| `chat_type`, `query_id` | поля мессенджера как есть |
| `fields` | все подписанные поля словарём |

Про `start_param` нужно помнить, что мессенджеры передают его по-разному. MAX кладёт значение в `initData`, и оно попадает в `data.start_param`. Telegram передаёт его в адресе страницы как `?startapp=...`, и читать его нужно на самой странице, как в примере выше.

### Два мессенджера, два токена

Проверка устроена одинаково для обоих мессенджеров, но токены у ботов разные, а подпись сделана токеном. Поэтому сервер должен проверять данные ботом того мессенджера, из которого открыта страница. Вот зачем страница присылает заголовок `X-Platform`:

```python
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from matele import escape
from matele.webapp import InitDataError

app = FastAPI()
bots = {"max": max_bot, "telegram": telegram_bot}


class Order(BaseModel):
    item: str


@app.post("/api/order")
async def order(order: Order, x_platform: str = Header(), x_init_data: str = Header()):
    bot = bots.get(x_platform)
    if bot is None:
        raise HTTPException(400, "unknown messenger")
    try:
        data = bot.verify_init_data(x_init_data)
    except InitDataError:
        raise HTTPException(401, "bad initData") from None
    if data.user is None:
        raise HTTPException(400, "no user")
    await bot.send_message(f"Заказ принят: <b>{escape(order.item)}</b>", user_id=data.user.id)
    return {"status": "ok"}
```

Заголовку `X-Platform` при этом верить не обязательно. Если страница соврёт о мессенджере, подпись просто не сойдётся: данные, подписанные токеном одного бота, не пройдут проверку токеном другого.

Без объекта бота то же самое делает функция `matele.webapp.verify_init_data(init_data, token, platform="max")`.

## Бот и сервер в одном процессе

Мини-приложению нужен веб-сервер, а боту нужно получать события. Удобнее всего держать их в одном процессе, чтобы сервер мог отправлять сообщения через те же объекты ботов.

Если бот работает на вебхуках, подключите `Webhook` в то же приложение, как описано в разделе [Вебхуки](webhooks.md#внутри-приложения-на-fastapi-или-starlette).

Если на polling, запустите его фоновой задачей при старте приложения:

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    polling = asyncio.create_task(dp.start_polling(max_bot, telegram_bot))
    yield
    polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)
    await max_bot.close()
    await telegram_bot.close()


app = FastAPI(lifespan=lifespan)
```

`start_polling` это тот же polling, что в `run_polling`, только он не блокирует программу, а работает как обычная async-задача, пока её не отменят.

## Обратная связь от приложения

Страница не может сама написать в чат. Всё, что должно появиться в чате, отправляет сервер через бота: `bot.send_message(..., user_id=data.user.id)`. Так делают подтверждения заказов и результаты заполненных форм.

Написать человеку бот может, только если тот уже открывал с ним чат. Для мини-приложения, открытого кнопкой бота, это всегда так.
