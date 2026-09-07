# Локальная проверка в Docker

Для локальной проверки Python-кода используйте `deploy/check-local.sh`. Скрипт
собирает Dockerfile stage `test`, а затем запускает внутри контейнера `ruff`,
`mypy`, `pytest` и сборку пакета. Python и pip на хосте для этого не нужны;
локальный `.venv` не используется и не изменяется.
Node.js для двух Python-тестов клиентского скрипта также установлен в test-образе.
Зависимости устанавливаются при сборке образа; сами проверки выполняются без сети.

```sh
deploy/check-local.sh
```

По умолчанию выполняются проверки и сборка test-образа. Отдельные операции:

```sh
deploy/check-local.sh build
deploy/check-local.sh check
deploy/check-local.sh export --out-dir /absolute/path/to/artifacts
deploy/check-local.sh all --out-dir /absolute/path/to/artifacts
```

Исходный репозиторий монтируется в контейнер только для чтения. Временные
файлы находятся в контейнерном tmpfs. Для `export` доступен только явно
указанный абсолютный каталог вне репозитория; wheel и sdist создаются в нём.
