# ZontAnalyzer

ZontAnalyzer безопасно читает телеметрию ZONT, хранит её локально, рассчитывает
объяснимые показатели отопления и формирует текстовый, JSON- или автономный HTML-отчёт.
При желании компактный пакет уже рассчитанных фактов можно передать OpenAI для интерпретации.

Приложение **никогда не меняет настройки ZONT и не управляет котлом**. В адаптере разрешены
только read-only методы `devices` и `load_data`; модель не получает инструментов управления.

Сейчас это рабочее автономное CLI-приложение и основа локального P2-пилота, проверенная на
реальной установке BAXI Connect+. Оно анализирует отопление, режимы, автоматический летний
переход, температуру и историческую цель ГВС, детерминированные эпизоды догрева и подтверждаемое
влияние приоритета ГВС на возврат отопления. Web-интерфейса и Telegram нет: основной канал
пилота — автоматически обновляемый standalone HTML.

## Первый отчёт

Ниже рекомендуемый на сегодня путь: локальный запуск без AI. Нужны Python 3.11+,
действующий ZONT token и контактный e-mail для обязательного заголовка `X-ZONT-Client`.

### 1. Установить приложение

Из корня репозитория:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mkdir -p .access reports
```

После повторного входа в терминал сначала снова выполните `. .venv/bin/activate`.

### 2. Подключить read-only доступ к ZONT

Рекомендуемый локальный вариант — `.access/zontaccesstoken.json`:

```json
{
  "token": "<ZONT token>",
  "email": "<контактный e-mail>"
}
```

```bash
chmod 600 .access/zontaccesstoken.json
```

Файл уже исключён из Git. Не присылайте token в чат, issue, лог или README. Вместо файла
можно использовать переменные окружения:

```bash
export ZONT_TOKEN='<ZONT token>'
export ZONT_CLIENT_EMAIL='<контактный e-mail>'
```

Также поддерживается `zontaccesstoken.json` в корне репозитория. Для любого secret-файла
обязательны права `0600`.

### 3. Проверить подключение

```bash
zont-analyzer init
zont-analyzer doctor --live
zont-analyzer discover
```

Ожидаемый результат:

- `init` показывает каталог данных, SQLite-файл и версию схемы;
- `doctor --live` заканчивается с `"ok": true`;
- `discover` находит контроллер, не печатая секреты и подробную конфигурацию.

По умолчанию всё постоянное состояние лежит в `.zont-analyzer/`, а база — в
`.zont-analyzer/zont-analyzer.sqlite3`.

### 4. Загрузить данные и открыть HTML

Для первой проверки достаточно семи дней:

```bash
zont-analyzer sync --backfill 7d
zont-analyzer analyze initial --no-ai --days 7
zont-analyzer report export --format html -o reports/latest.html
```

Откройте `reports/latest.html` обычным браузером. Флаг `--no-ai` гарантирует, что анализ
полностью локальный. Когда этот путь проверен, можно расширить историю:

```bash
zont-analyzer sync --backfill 90d
zont-analyzer analyze initial --no-ai --days 30
zont-analyzer report export --format html -o reports/latest.html
```

Backfill возобновляемый: если ZONT временно вернул `429` или оборвал соединение, повторите ту
же команду. Уже сохранённые точки не дублируются.

## Что смотреть в отчёте

Отчёт последовательно показывает:

1. период и качество данных — при плохом покрытии менять отопление по отчёту не следует;
2. текущий режим, отопительную уставку и фактическое автоматическое летнее состояние;
3. температуру помещения относительно активной уставки и наружную температуру;
4. состояние и качество ГВС: активную цель, время восстановления, перелёт и эпизоды догрева;
5. подтверждённые паузы/возврат отопления после ГВС, остаточное тепло и горячий хвост подачи;
6. наблюдавшиеся факты отдельно от многосигнальных выводов и недоказанных гипотез;
7. безопасные рекомендации с evidence ID, рисками и условиями остановки.

Рекомендация — это подсказка для ручной проверки, не команда котлу. `mark-applied` лишь
записывает в локальную БД, что владелец сам сделал изменение.

Посмотреть последний отчёт в терминале:

```bash
zont-analyzer report latest
```

Проверить свежесть и целостность локального состояния:

```bash
zont-analyzer status
```

## Ежедневное использование

Ручной цикл за завершившиеся вчерашние сутки:

```bash
zont-analyzer sync
zont-analyzer analyze daily --no-ai
zont-analyzer report export --format html -o reports/latest.html
```

Для конкретной локальной даты:

```bash
zont-analyzer analyze daily --date 2026-08-01 --no-ai
```

Проверить одну итерацию фонового процесса:

```bash
zont-analyzer run --once
```

Оставить синхронизацию работать в foreground:

```bash
zont-analyzer run
```

Остановка — `Ctrl+C`. Worker синхронизирует данные с интервалом из конфигурации, догоняет
пропущенные завершённые локальные дни, пересчитывает вчера с учётом поздних данных и атомарно
обновляет `reports/latest.html` вместе с архивом `reports/daily/YYYY-MM-DD.{html,json}` внутри
каталога состояния. Неполный sync считается ошибкой, а `worker-status.json` и команда
`healthcheck` позволяют отличить работающий процесс от зависшего. Перед постоянным запуском
обязательно проверьте `run --once`.

## OpenAI — необязательно

Сначала обязательно получите нормальный локальный отчёт с `--no-ai`. Затем создайте
`config.yaml` с минимальным override:

```yaml
openai:
  enabled: true
```

Передайте ключ через environment:

```bash
export OPENAI_API_KEY='<project API key>'
zont-analyzer analyze initial --days 30
```

Для локальной разработки ключ также можно положить одной строкой в
`.access/.openai_access_token.txt` и выполнить `chmod 600` для файла.

В OpenAI отправляются не сырые временные ряды, а компактные локально рассчитанные
quality/metric/event факты и контекст. Провайдер использует строгую Pydantic-схему,
`store=False`, не включает tools/functions, проверяет evidence ID и соблюдает месячный лимит
токенов. Если AI недоступен, локальный отчёт всё равно сохраняется.

## Конфигурация и каталоги

`config.yaml` необязателен. Встроенные defaults и discovery достаточны для первого запуска.
Чтобы изменить конкретные параметры, скопируйте [config.example.yaml](config.example.yaml) и
оставьте только нужные overrides. Неизвестные YAML-поля считаются ошибкой; секреты в YAML не
хранятся.

Показать effective config и наличие секретов без их значений:

```bash
zont-analyzer config explain
```

Другой каталог постоянных данных:

```bash
zont-analyzer --data-dir /srv/zont-analyzer status
```

Или задайте `ZONT_ANALYZER_DATA_DIR`. Глобальные параметры `--data-dir`, `--config` и `--verbose`
ставятся **до** имени команды.

Локальная структура по умолчанию:

```text
.access/                         secrets, Git-ignored
.zont-analyzer/
  zont-analyzer.sqlite3         телеметрия, отчёты и служебное состояние
  backups/                      online backup по умолчанию
  worker-status.json            heartbeat и результат последнего worker-цикла
  reports/
    latest.html                 стабильный последний завершённый суточный отчёт
    daily/                      датированные HTML/JSON
```

Создать проверенную online-копию SQLite:

```bash
zont-analyzer db backup
```

Эта копия остаётся на том же диске. Для production нужен отдельный off-host backup.

## Docker и запуск 24/7

Docker image всегда использует `/data` для постоянного состояния. Не запускайте его без явного
volume: иначе Docker создаст анонимный том, который легко потерять.

Команды ниже ожидают, что `ZONT_TOKEN` и `ZONT_CLIENT_EMAIL` уже экспортированы в shell.
Локальная `.access/` намеренно не попадает в image.

```bash
docker build -t zont-analyzer:local .
docker volume create zont-analyzer-data

docker run --rm \
  -e ZONT_TOKEN -e ZONT_CLIENT_EMAIL \
  -v zont-analyzer-data:/data \
  zont-analyzer:local init

docker run --rm \
  -e ZONT_TOKEN -e ZONT_CLIENT_EMAIL \
  -v zont-analyzer-data:/data \
  zont-analyzer:local doctor --live

docker run --rm \
  -e ZONT_TOKEN -e ZONT_CLIENT_EMAIL \
  -v zont-analyzer-data:/data \
  zont-analyzer:local sync --backfill 7d

docker run --rm \
  -e ZONT_TOKEN -e ZONT_CLIENT_EMAIL \
  -v zont-analyzer-data:/data \
  zont-analyzer:local analyze initial --no-ai --days 7
```

После проверки запустите worker:

```bash
docker run -d --name zont-analyzer --restart unless-stopped \
  -e ZONT_TOKEN -e ZONT_CLIENT_EMAIL \
  -v zont-analyzer-data:/data \
  zont-analyzer:local

docker logs -f zont-analyzer
```

Посмотреть отчёт и status:

```bash
docker exec zont-analyzer zont-analyzer --data-dir /data report latest
docker exec zont-analyzer zont-analyzer --data-dir /data status
```

Проверить heartbeat и найти автоматически опубликованный HTML:

```bash
docker exec zont-analyzer zont-analyzer --data-dir /data healthcheck
docker exec zont-analyzer sh -c 'test -s /data/reports/latest.html'
```

Named volume переживает удаление контейнера и обновление image. Его удаляют только явные
`docker volume rm`, `docker compose down -v` или pruning. Bind mount вроде
`/srv/zont-analyzer:/data` удобнее, если БД и backup должны быть видны на хосте; каталог должен
быть доступен непривилегированному пользователю `zont` внутри image.

Для ручного Compose-деплоя на shared-сервере используйте
[deploy/OPERATIONS.md](deploy/OPERATIONS.md). Базовый Compose не публикует порты и не содержит
доменов; host-specific bind каталога сайта находится только во временном test override.

## Команды

| Команда | Назначение |
| --- | --- |
| `init` | Создать каталог состояния и привести БД к текущей схеме. |
| `doctor [--live]` | Проверить config, секреты, БД и, с `--live`, read-only доступ к ZONT. |
| `status` | Показать размер/целостность БД, диапазон данных и число отчётов. |
| `healthcheck` | Проверить свежесть heartbeat и состояние worker. |
| `discover` | Обновить безопасный снимок оборудования и конфигурации. |
| `sync [--backfill 90d]` | Догрузить историю с overlap и durable cursor. |
| `analyze initial\|daily\|weekly\|monthly\|seasonal` | Рассчитать и сохранить отчёт. Долгие периоды пока экспериментальны. |
| `report latest\|show\|export` | Посмотреть или экспортировать сохранённый отчёт. |
| `recommendations list\|show\|mark-applied\|reject` | Вести локальный lifecycle рекомендаций. |
| `config explain\|export-inferred` | Проверить effective config или inferred profile. |
| `notifications test` | Доставить pending-отчёты в текущий log-канал. |
| `db backup` | Создать и проверить online backup SQLite. |
| `run [--once]` | Выполнить sync, catch-up daily-отчётов и атомарную публикацию `latest.html`. |

Детальную справку даёт `zont-analyzer --help` или, например,
`zont-analyzer analyze daily --help`.

## Частые проблемы

- **`Secret file ... must have mode 0600`** — выполните `chmod 600 <файл>`.
- **`missing token or client email`** — проверьте оба поля файла или обе environment variables.
- **Sync завершился после `429`/сетевой ошибки** — повторите ту же команду; cursor не продвигается
  после неполного окна.
- **В отчёте нет комнатных метрик** — связь отопительного контура с комнатным датчиком не была
  определена уверенно; проверьте `discover` и `config export-inferred`, не назначайте случайный
  датчик комнатным.
- **Низкое качество данных** — сначала восстановите сбор и накопите непрерывный период; настройки
  котла по такому отчёту менять не следует.
- **`No reports exist`** — сначала выполните `sync` и одну из команд `analyze`.

## Разработка

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy src/zont_analyzer
```

SQLite использует WAL, foreign keys, busy timeout и канонические UPSERT-ключи. При переходе на
новую Alembic revision приложение сначала делает и проверяет online backup. CI дополнительно
проверяет сборку wheel, импорт установленного пакета и Docker image.

Документы проекта:

- [актуальный план](.codex/roadmap.md) — приоритеты и критерии готовности;
- [краткий фактический статус](.codex/status.md) — что реально проверено;
- [архитектурная спецификация](.codex/architecture.md) — принципы и долгосрочные границы;
- [действия владельца](.codex/for_human.md) — секреты и production-решения.
