# Мини-приложения

Мини-приложение это веб-страница, которая открывается прямо в мессенджере по кнопке бота. На ней можно сделать то, что неудобно делать кнопками: каталог, форму, календарь.

Готовый пример со страницей и сервером на FastAPI лежит в [examples/mini_app](https://github.com/cvsqwe/matele/tree/main/examples/mini_app).

## Кнопка

```python
from matele import Button, Keyboard

keyboard = Keyboard([
    [Button("Открыть магазин", app="")],
    [Button("Сразу к кофе", app="coffee")],
])
await ctx.reply("Магазин открывается прямо в мессенджере:", keyboard)
```

Значение `app` приложение получает как параметр запуска, по нему удобно сразу открыть нужную страницу. Пустая строка значит «без параметра». В значении допускаются латинские буквы, цифры, `_` и `-`, не больше 512 символов.

Адрес страницы задаётся по-разному:

- в MAX адрес мини-приложения указывается в настройках бота на [business.max.ru](https://business.max.ru);
- в Telegram его передают боту: `TelegramBot(token, app_url="https://shop.example.com/")`.

В Telegram такие кнопки работают только в личных чатах.

## Страница

На странице подключается скрипт мессенджера. Он даёт доступ к `initData`: подписанным мессенджером данным о человеке, который открыл приложение.

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

Точный код страницы для обоих мессенджеров есть в [index.html из примера](https://github.com/cvsqwe/matele/blob/main/examples/mini_app/index.html).

## Проверка на сервере

Никогда не верьте id пользователя, который страница прислала сама: без проверки подписи кто угодно может выдать себя за кого угодно. Проверяйте `initData` в каждом запросе:

```python
from matele.webapp import InitDataError

try:
    data = bot.verify_init_data(request.headers["X-Init-Data"])
except InitDataError:
    return 401

await bot.send_message("Заказ принят", user_id=data.user.id)
```

`verify_init_data` есть и у `MaxBot`, и у `TelegramBot`. Она проверяет подпись токеном бота и срок: по умолчанию данные действительны час, как советует MAX. Срок меняется параметром `max_age` в секундах, а `max_age=None` отключает проверку срока.

В результате (`WebAppData`):

- `user`: кто открыл приложение (`User` с `id`, `name`, `username`);
- `start_param`: в MAX значение `app` у кнопки. Telegram кладёт его не в `initData`, а в адрес страницы (`?startapp=...`);
- `language`: язык интерфейса человека, например `"ru"`;
- `auth_date`: когда мессенджер подписал данные;
- `chat_type`, `query_id` и `fields`: остальные поля как есть.

Проверка устроена одинаково для обоих мессенджеров, но токены у ботов разные. Поэтому страница должна сообщать серверу, из какого она мессенджера (в примере это заголовок `X-Platform`), а сервер должен проверять данные ботом этого мессенджера.

Без бота можно проверить функцией `matele.webapp.verify_init_data(init_data, token, platform="max")`.
