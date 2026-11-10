# Файлы и форматирование

## Форматирование текста

Текст сообщений отправляется как HTML в обоих мессенджерах:

```python
await ctx.reply("<b>Жирный</b>, <i>курсив</i>, <code>код</code> и <a href=\"https://example.com\">ссылка</a>")
```

Теги, которые понимают оба мессенджера: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>` и `<a href="...">`.

Если вставляете в сообщение то, что прислал человек, экранируйте это, иначе символы `<`, `>` и `&` в его тексте сломают разметку:

```python
import html

await ctx.reply(f"Вы написали: <b>{html.escape(ctx.text)}</b>")
```

Если Telegram всё же не смог разобрать HTML, matele отправит то же сообщение простым текстом, чтобы оно не потерялось.

## Отправка файлов

```python
from pathlib import Path

await ctx.reply_file(Path("cat.jpg"), kind="image", caption="Кот")
await ctx.reply_file(Path("clip.mp4"), kind="video")
await ctx.reply_file(Path("song.mp3"), kind="audio")
await ctx.reply_file(report_bytes, filename="report.csv")
```

`kind` бывает `"image"`, `"video"`, `"audio"` и `"file"` (по умолчанию). Файл можно передать:

- путём `Path`: имя файла возьмётся из пути;
- байтами: имя лучше указать в `filename`, иначе будет стандартное вроде `image.jpg`;
- открытым файлом в двоичном режиме.

Путь и открытый файл отправляются потоком и не читаются в память целиком, так что большие файлы не займут лишнюю память. Ограничения на размер задают мессенджеры.

У `reply_file` есть `keyboard` и `quote`, как у `reply`. В Telegram подпись длиннее 1024 символов уходит отдельным сообщением.

## Ответ на сообщение

`quote=True` отправляет сообщение ответом на сообщение человека (или на сообщение, где нажали кнопку):

```python
await ctx.reply("Принято", quote=True)
```

## Статус «печатает…»

Если ответ готовится долго, покажите, что бот работает:

```python
async with ctx.typing():
    report = await build_report()
await ctx.reply_file(report, filename="report.pdf")
```

Статус обновляется каждые несколько секунд, пока выполняется блок. Если мессенджер не принял запрос статуса, хендлер это не прервёт.

## Входящие файлы

Файлы, которые прислал человек, лежат в `ctx.message.attachments`. У каждого вложения есть `type` (`"image"`, `"video"`, `"audio"`, `"file"`, а в Telegram ещё `"voice"`, `"animation"` и `"video_note"`), `filename` и `token`.

```python
from matele import HasAttachment


@dp.message(HasAttachment("file", "image"))
async def got_file(ctx: Context):
    att = ctx.message.attachments[0]
    content, filename = await ctx.bot.download_file(att.token, file_type=att.type, filename=att.filename, url=att.url)
    await storage.save(filename, content)
    await ctx.reply(f"Сохранил {filename}, {len(content)} байт")
```

Файл скачивается в память целиком, поэтому размер ограничен: по умолчанию 50 МБ, файл больше вызовет `BotApiError` с кодом 413. Лимит задаётся при создании клиента, например `MaxBot(token, api=MaxApi(token, max_download=200 * 1024 * 1024))`. `max_download=None` снимает ограничение.

Переслать файл без скачивания можно по его токену. Токен работает только в том мессенджере, откуда пришёл файл:

```python
await ctx.bot.send_file_by_token(att.token, user_id=manager_id, file_type=att.type, caption="Новый документ")
```

## Отправка без хендлера

Методы `ctx` работают внутри хендлера. Вне его, например в планировщике, пишите через сам бот. Получатель задаётся либо `user_id=` (личный чат человека), либо `chat_id=` (группа):

```python
max_bot = MaxBot(MAX_TOKEN)

await max_bot.send_message("Напоминание о встрече", user_id=123)
await max_bot.send_file(Path("plan.pdf"), chat_id=-100, caption="План на неделю")
```

Чтобы написать человеку сразу во все его мессенджеры, используйте [рассылки](broadcasts.md).
