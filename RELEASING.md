# Как выпустить версию

Один раз, перед первым релизом:

1. Зайти на https://pypi.org/manage/account/publishing/ и добавить «pending publisher»: проект `matele`, владелец `cvsqwe`, репозиторий `matele`, workflow `release.yml`, environment `pypi`.
2. В настройках репозитория на GitHub создать environment `pypi` (Settings, Environments).

Каждый релиз:

1. Поменять `__version__` в `src/matele/__init__.py`, например на `0.1.0`.
2. В `CHANGELOG.md` переименовать «Не выпущено» в номер версии с датой.
3. Закоммитить, поставить тег и отправить:

```bash
git tag v0.1.0
git push origin main v0.1.0
```

GitHub Actions соберёт пакет, проверит, что тег совпадает с версией, и опубликует его на PyPI.
