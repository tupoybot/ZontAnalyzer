# M0 — контракты и предварительная реализуемость

Дата: 2026-09-13. Статус: **в работе, не принят**. Ветка `docs/cloud-first-m0`
от `architecture/cloud-first-serverless`; application baseline — `main`, `82a7a82`.
Исходники приложения между этими ветками совпадают. Этот документ определяет
будущие контракты; интерфейсы/YDB/runtime adapter здесь не реализованы.
Порядок работ и приёмки — [cloud-first план](./cloud_first_milestone.md).

## Архитектурные решения и готовность

- Телеметрия загружается при генерации отчёта: report job → нужное окно ZONT
  → проверка полноты → анализ → публикация. Отдельного регулярного сборщика нет.
- YDB — целевая БД; SQLite поддерживается до завершения перехода и rollback.
- Внешние HTTP-клиенты используют управляемый proxy с allowlist и без direct
  fallback. Сетевая изоляция произвольного кода контейнера не требуется.
- Облачные ресурсы приложения размещаются в отдельном project folder.
  Имена, идентификаторы и адреса окружения задаются приватной конфигурацией.
- Read-only проверки доступа, billing и основных квот выполнены. Подтверждение
  bootstrap permissions и остальных квот требуется перед provisioning M1.
  Облачные ресурсы в рамках M0 не создавались.

Публичный документ содержит контракты и итог проверок. Инвентарь аккаунта,
сетевую топологию, адреса узлов и финансовые ограничения владельца не публиковать.

## Границы приложения и конфигурации

Один application image из принятого `main` по digest используется на VPS и в cloud.
Код совместимости с облаком сначала проходит отдельную приёмку в `main`, затем
новый digest выбирается инфраструктурой. M1 может иметь отдельный синтетический
probe artifact, явно обозначенный как probe; он не доказывает совместимость
production image и не запускает аналитику. Отдельной cloud-копии приложения нет.

Будущий composition root выбирает `storage.backend = sqlite | ydb`, publisher,
invocation adapter, config/secrets provider и egress policy. SQLAlchemy Session,
YDB SDK и cloud identities не входят в domain/analytics контракты. Сейчас
[Runtime](../src/zont_analyzer/runtime.py) напрямую создаёт `Database`, выполняет
миграцию и обслуживание рекомендаций; этот startup не годится как чистая проверка
identity/config и должен быть разделён в M4.

`ConfigProvider.load(environment, version)` возвращает валидированную конфигурацию
и происхождение значений; `SecretProvider.resolve(reference)` — секрет в памяти.
Имена методов — проектные обозначения, не существующий API. Локально сохраняются
files/env и проверка режима 0600; cloud использует Lockbox references на конкретные
версии. Значения секретов не попадают в image, Git, Terraform state или логи.
Неизвестный backend, отсутствующий обязательный секрет и неполная egress policy
блокируют соответствующий запуск. Cloud не читает случайные локальные fallback-файлы.

Shadow/pilot имеет отдельные БД, bucket/prefix, secrets, job/usage namespace и
неактивное production-расписание. AI generation, сообщения и production-записи
выключены явно. `run --once` сам по себе не является режимом без побочных эффектов.

## Карта сохраняемых данных и repository contracts

Полный перечень текущих таблиц ниже включает модели, объявленные в application,
а не только в SQLite adapter. Идентификаторы, связи, история и аудит сохраняются.
Repository возвращает доменные объекты/DTO и bounded pages с устойчивым cursor;
никаких ORM rows, сессий или произвольного SQL на прикладной границе.

| Контракт | Текущие таблицы | Операции и гарантии |
| --- | --- | --- |
| CatalogRepository | `devices`, `entities`, `config_snapshots`, `telemetry_series` | Discovery/upsert, snapshots, роли/provenance, список устройств/серий; естественные ключи устройств/источников не меняются |
| TelemetryRepository | `telemetry_samples`, `ingestion_cursors`, `data_gaps`, `source_events` | Идемпотентные batch upsert/delete, чтение `[start,end)`, numeric/text/quality; курсор не обгоняет сохранённые данные; late data меняет revision |
| ReportRepository | `analysis_periods`, `metric_values`, `detected_events`, `reports` | Атомарное сохранение графа отчёта, latest/prior/completed, compare-and-set служебных markers; история AI и исходные поля сохраняются |
| FeedbackRepository | `recommendations`, `interventions`, `intervention_experiments` | Lifecycle, feedback, история и ручные эксперименты; статус, intervention и snapshot согласованы транзакционно |
| OwnerRepository | `owner_profile_revisions`, `gas_readings`, `gas_meter_boundaries`, `gas_reading_audit` | История профиля, auto/manual provenance, показания и перенос даты, сегменты счётчика; валидация и аудит в одной транзакции, запрет перезаписи чужой даты |
| TariffRepository | `gas_tariffs`, `gas_tariff_audit` | История по `(scope,effective_month)`, цена/валюта/дата вступления, атомарный аудит; новые тарифы не подменяют исторические настройки |
| AISettingsRepository | `ai_settings_revisions` | Snapshot/effective config/save с версиями и before-state |
| ModelReviewRepository | `model_review_state`, `model_review_runs`, `model_review_proposals` | Due/claim/result/proposal/decision; применение предложения и ревизия AI-настроек атомарны |
| JobRepository / UsageRepository (расширение) | `jobs`, `llm_calls`, `notification_outbox` | Сейчас есть учёт вызовов/outbox, но `jobs` не используется для claim. Целевой контракт добавляет claim/idempotency и состояния неизвестного внешнего исхода |
| RevisionRepository / PublicationRepository | `app_meta`, `publication_changes` | Period fingerprint, metadata, coalesced invalidation, bounded чтение журнала и publication checkpoint; не терять изменения при конкуренции |

Источники: [основные модели и операции](../src/zont_analyzer/adapters/sqlite/database.py),
[профиль/газ](../src/zont_analyzer/application/owner_context.py),
[тарифы](../src/zont_analyzer/application/gas_tariffs.py),
[AI settings](../src/zont_analyzer/application/ai_settings.py),
[пересмотр моделей](../src/zont_analyzer/application/model_review.py),
[журнал публикации](../src/zont_analyzer/adapters/sqlite/publication_journal.py).
`alembic_version` — операционная история SQLite; YDB получает собственную схему
миграций, а не выполнение Alembic SQL без адаптации. SQLite head: `f1a2b3c4d5e6`.

### Типы и идентичность

| Доменный тип | Текущий SQLite | Целевой контракт YDB для проверки в M3 |
| --- | --- | --- |
| Строковый ID/ключ/enum | `String`/`TEXT` | `Utf8`; без изменения регистра, префиксов и existing IDs |
| Unix UTC seconds | `Integer` | `Int64`, точная единица seconds и границы `[start,end)` |
| Время действия/записи | SQLAlchemy DateTime с UTC-нормализацией | `Timestamp` либо точное целое число микросекунд; выбрать физический тип по round-trip M3, без потери существующей точности |
| Локальный день/месяц | ISO-строка | `Utf8` с прежней валидацией; timezone дома отдельно, день не равен UTC midnight |
| Газ/денежная цена | Decimal как строка | `Utf8` с каноническим Decimal, не Double; масштаб и округление прежние |
| Измерение | nullable Float/text | nullable `Double`/`Utf8`; сохранять numeric/text/quality и различие null/пустой строки/нуля |
| JSON | сериализованный `TEXT` | `Utf8` с прежней сериализацией; hashes вычисляет общее приложение, не DB JSON normalization |
| Boolean | SQLAlchemy Boolean | `Bool`, без неявного строкового преобразования |
| Порядковый ID/revision | autoincrement Integer | Сохранить импортированные значения; последующие Int64 выдавать транзакционным allocator; revision имеет явно монотонный порядок |

Порядок строк, участвующих в fingerprint, задаётся явно: `(series_id,timestamp)`.
Сохраняются версии fingerprint, empty sentinel и legacy-marker правила.
Settings revision входит в provenance; publication revision — high-water mark.
Поэтому нельзя заменить все числовые ID случайными UUID без отдельной миграции
контракта. Allocator/журнал могут конфликтовать; нагрузка и retries проверяются M3.

### Транзакции и особенности SQLite

- `save_report` сохраняет период, metrics/events, report, рекомендации и outbox
  вместе; публикационная invalidation обязана быть в той же транзакции.
  Сейчас это обеспечивают SQLite triggers; YDB adapter записывает journal явно.
- Газ/профиль/тариф/AI decision используют read-validate-write и аудит. В YDB
  нужен serializable read/write либо проверяемый CAS, а не отдельные read и write.
- Идемпотентный ключ задания, owner token, lease expiry и fencing version
  захватываются атомарно. Завершить job/checkpoint может только текущий владелец.
  Lease не обеспечивает ровно один внешний AI-вызов.
- DB retry повторяет только транзакцию без HTTP/публикации внутри callback.
  Сетевой вызов записывается как prepared → in-flight → succeeded/failed/unknown;
  unknown требует reconciliation/решения, не повторной генерации по таймауту.
  Этот протокол — новая реализация M4, текущий `llm_calls` его не обеспечивает.
- `BEGIN IMMEDIATE`, WAL, PRAGMA, SQLite `ON CONFLICT`, `RETURNING`, autoincrement,
  FK/cascade и триггеры принадлежат SQLite adapter. YDB воспроизводит гарантии,
  не SQL. Integrity/uniqueness проверяются транзакционно там, где нет эквивалентного
  DB constraint; отсутствие orphan rows входит в приёмку.
- Сейчас SQLAlchemy модели/запросы есть в owner/tariff/AI/review application stores;
  `incremental_publication.py` читает canonical таблицы прямым SQL. Их требуется
  вынести за contracts в M4. Заменить только класс `Database` недостаточно.
- Чтение repository использует `[start,end)`, а ingestion сейчас принимает
  граничную точку `timestamp == end` с последующим идемпотентным upsert. Сохранить
  полноту на стыках окон и отдельно проверить эти границы, не обрезать данные
  механическим применением фильтра чтения к ответу ZONT.

## Publisher и восстановление

`ArtifactPublisher.stage(report_id, version, artifacts)` загружает полный immutable
набор HTML/JSON. `commit_manifest(expected_version, next_manifest)` публикует только
ссылки на уже готовые объекты. `latest` указывает только на последний daily.
Отказ между загрузкой и commit допускает неиспользуемые объекты, но не битые ссылки.
Между YDB и Object Storage нет общей транзакции: publication job и подтверждение
manifest восстанавливаются идемпотентно, checkpoint обновляется после публикации.
Fencing защищает checkpoint; для защиты manifest от устаревшего writer нужен
проверенный conditional write/CAS либо сериализующий publish coordinator.
Одной проверки lease перед обычным PUT недостаточно; механизм доказывается M5.

Текущий publisher использует `flock`, `fsync`, `os.replace` и локальные paths.
Отдельный `.publication-cache.sqlite3` содержит восстановимый индекс/очередь,
не второй источник canonical данных. Его не импортировать как business state:
cloud publisher использует долговечные index/queue/checkpoint в YDB, восстанавливаемые
из canonical reports и manifest. Ограничение восемь отчётов за проход и приоритет
последнего daily сохраняются. Пустая очередь не должна перечитывать весь архив.
Источник: [инкрементальная публикация](./incremental-publication.md).

## Сохраняемые web/API операции

Относительные пути ниже привязаны к настраиваемому API base, сейчас `/api`.
Источник: [HTTP handler](../src/zont_analyzer/application/feedback.py).

| Путь | Методы | Контракт |
| --- | --- | --- |
| `/equipment`; `/equipment/{device}` | GET для списка; GET/PUT для устройства | Список/профиль, история параметров и provenance; PUT списка возвращает 405 |
| `/reports/{report}/gas` | GET, PUT | Показание с независимой датой и аудитом; ответ без ожидания HTML |
| `/gas-tariffs` | GET, PUT | История/изменение тарифа и валюты |
| `/recommendations/{id}/feedback` | GET, PUT | Feedback/status/note/ручной experiment |
| `/ai`, `/ai/review` | GET, PUT | Настройки, review/proposal/decision, сохранение происхождения AI |
| `/reports/{id}/regenerate` | GET, POST | Сохраняются статус и 202; cloud добавляет долговечную регистрацию/доставку вместо нынешнего thread + file lock |
| `/health`, `/worker-health` | GET | Liveness отдельно от свежести worker; cloud freshness по долговечному heartbeat |
| HTML/JSON archive, `reports.json`, `latest.html` | GET | Приватная публикация и прежняя навигация, без directory listing |

Нужно сохранить HTTP-коды/валидацию, trusted identity и same-origin правила.
Basic Auth сейчас обеспечивается внешним reverse proxy. Serverless Containers
[удаляет Authorization и Cookie](https://yandex.cloud/en/docs/serverless-containers/concepts/invoke),
поэтому прямой перенос nginx-схемы не доказан. Кандидат M1 web-spike — API Gateway
с авторизацией на входе, приватным backend и явной передачей доверенного контекста.
Поддельный identity header от клиента не принимается; raw container URL не даёт
анонимный доступ к данным. Секреты и Terraform state не лежат в publication bucket.

## Invocation и временные бюджеты

Пересматриваемая модель: один image и несколько явно выбираемых use cases,
без обязательного запуска полного `PilotService.run_cycle` на каждый timer.
Это предложение M0, требующее согласования свежести и доказательства M2:

| Причина запуска | Работа | Что не выполняется без отдельной причины |
| --- | --- | --- |
| Завершённый локальный день после 01:00 | Report job получает нужную историю ZONT, проверяет полноту, выполняет daily analysis и публикацию | Повторный AI при изменении только служебных полей |
| Завершение недели/месяца/сезона | Получение недостающей истории/контекста и соответствующий report job | Повторный сбор уже сохранённой полной истории |
| Изменение газа/профиля/тарифа/feedback | Транзакционная запись + publication outbox | Ожидание полного анализа в HTTP ответе |
| Вопрос/ручная регенерация | Долговечное задание по конкретному report, загрузка нужных данных перед анализом | Телеметрия/AI по простому GET страницы |
| Срок review моделей | Отдельный due job, либо проверка due в daily dispatcher | Фоновый thread на каждом invocation |

Outbox доставляет wake-up после commit с идемпотентным job ID. Редкий recovery
проход находит пропущенные wake-up; нельзя подтверждать HTTP-запись и надеяться
на thread, который платформа заморозит. Выбор native async invocation или queue
зависит от гарантии доставки/стоимости, ещё не повод вводить отдельные микросервисы.
Daily запуск учитывает timezone дома, DST и прошлые пропущенные периоды.
Термин sync ниже обозначает внутреннюю загрузку данных report job, не отдельный timer.
Отдельного периодического sync нет. Повторное задание продолжает cursor;
при отказе источника предыдущий опубликованный отчёт остаётся доступен.

История телеметрии уже читается от cursor с overlap и окнами до 24 часов: частота
получения и гранулярность исходных измерений не одно и то же. Однако discovery
каждого sync фиксирует текущую конфигурацию: редкие запуски могут пропускать её
промежуточные изменения. M2 должен отдельно проверить архив/late data и сведения
о смене настроек. Ежедневный sync нельзя объявить эквивалентом пятиминутного
сбора snapshots без этой проверки. Это проверка источника в M2, не основание
сохранять пятиминутный polling без отдельной продуктовой потребности.

Предложение: request-driven HTTP server на `PORT`, Timer Trigger вызывает его
напрямую. На каждый запрос выполняется bounded one-shot use case, затем HTTP
response; процесс может обслужить следующий warm invocation. Long-lived worker
с внутренним sleep для cloud не используется. Provisioned instances = 0.

Предлагаемый runtime timeout 300 s: 30 s cold start/config, до 240 s работы,
30 s на фиксацию/ответ. Это целевые бюджеты для M1/M2, не измеренные значения.
M1 smoke ограничить 60 s, ручные вызовы, без production scheduler/AI/ZONT.
Initial concurrency = 1 и узкий instance limit; корректность всё равно опирается
на job claim, поскольку retries/перекрытия возможны.

Внутри 240 s не запускать новую порцию, если оставшегося времени недостаточно
для её верхнего бюджета и commit. Sync делится по устройству/серии/окну с cursor;
catch-up/длинные отчёты и review — возобновляемые jobs, не бесконечный HTTP request.
ZONT: connect 5 s, запрос до 30 s, bounded retry/backoff только пока хватает deadline.
AI: connect 5 s, общий вызов до 120 s, автоматические retries = 0. Каталог моделей:
до 15 s на запрос. DB/cloud operation: до 10 s на попытку, не более 2 повторов в
оставшемся общем deadline; числа уточнить измерениями M2/M3.

Текущий ZONT client делает до трёх попыток и ждёт Retry-After до 60 s между ними;
общего deadline нет. OpenAI использует 120 s/max_retries=0. Пересмотр моделей и
regeneration запускают локальные threads, что не является долговечной cloud job.
Эти различия должны быть устранены в compatibility releases, прежде чем M4
подтвердит фактическую работу одного image на VPS и в cloud.

## Egress и внешние условия

Решение владельца 2026-09-13: обеспечить выбранный proxy-маршрут HTTP-клиентов,
без обязательной сетевой изоляции произвольного кода контейнера. Сохраняются
allowlist, явные direct-исключения для cloud APIs и отсутствие direct fallback.
Это уточнение исходного
[контракта M0](./cloud_first_milestone.md#контракт-проксирования-и-контроля-трафика).
[Документация сети](https://yandex.cloud/en/docs/serverless-containers/concepts/networking)
сохраняет internet access при подключении VPC. Поэтому гарантируется поведение
прикладных клиентов, не containment скомпрометированного runtime.

Предварительная таблица маршрутов: ZONT `my.zont.online:443` и OpenAI generation/
metadata `api.openai.com:443` — через выбранный proxy; каталог
`developers.openai.com:443` — через proxy; конкретные YDB/Object Storage/Lockbox/
monitoring endpoints и служебная identity — явно разрешённый direct. Прочие
назначения deny. Redirect на новое назначение проходит повторную проверку.
Переменные `NO_PROXY`/окружения не могут менять политику приложения. Отказ
обязательного proxy блокирует вызов, direct fallback отсутствует.

Выбран существующий внешний egress. Предлагаемый интерфейс приложения —
аутентифицированный TLS CONNECT с allowlist. Подключение, лимиты, восстановление
и дополнительную стоимость необходимо проверить до реализации; адреса и
устройство личной инфраструктуры в публичный контракт не входят.

Размещение и доступность OpenAI проверяются по
[официальному списку](https://developers.openai.com/api/docs/supported-countries)
и фактической схеме доступа. M2 сначала проверяет DNS/TLS/маршрут без ключей,
затем выполняет не более одного аутентифицированного запроса за пользовательскую
итерацию. В M0 запросов OpenAI: 0.

## Предварительная стоимость

Ниже сценарии выбранного запуска по отчёту, **не замеры**: 30 дней, 30 daily jobs,
1 CPU, 0,5 GiB RAM,
без provisioned instances. Ставки 2026-09-13: CPU 5,69 RUB/core-hour,
RAM 3,79 RUB/GiB-hour, вызовы 18,97 RUB/миллион. Учтены отдельно варианты с
полностью доступными бесплатными 5 CPU-hour/10 GiB-hour/миллионом вызовов и без них.
Источник: [тариф Serverless Containers](https://yandex.cloud/ru/docs/serverless-containers/pricing).

| Суммарное время одного daily job, включая cold start/сеть | Compute с free tier, RUB | Compute без free tier, RUB |
| --- | --- | --- |
| 60 s | 0,00 | 3,79 |
| 300 s | 0,00 | 18,96 |
| 600 s, несколькими bounded порциями | 0,00 | 37,93 |

Дополнительные месячные/недельные, ручные, publication и recovery jobs считаются
по фактическому времени отдельно. Для примера: ещё 10 CPU-hours при 0,5 GiB RAM —
75,85 RUB без free tier плюс вызовы. Отдельный периодический сбор
в эту оценку не входит.

Для небольшого стенда дополнительно: три версии Lockbox secrets на 720 часов
стоят 59,18 RUB, 30 тысяч чтений — 11,37 RUB. Registry 1 GiB на 720 часов —
3,29 RUB. Object Storage standard — 0,0033 RUB/GiB-hour после free tier;
PUT/LIST — 0,5692 RUB/тысячу, GET — 0,46 RUB/10 тысяч после free tier.
Источники: [Lockbox](https://yandex.cloud/ru/docs/lockbox/pricing),
[Registry](https://yandex.cloud/ru/docs/container-registry/pricing),
[Storage](https://yandex.cloud/ru/docs/storage/pricing), read-only
[Billing SKU API](https://yandex.cloud/en/docs/billing/api-ref/Sku/list).
SKU: `dn2j3lv6ln3prgf1sotn`, `dn2v4cf61h9ogav4id0n`, `dn25odoxu3dfmjrnzxjy`,
`dn2blgm6egk7u97t0mr0`, `dn2c17otu20ij2eu4k18`, `dn2tsb3bn2sgk6tict7r`.

YDB RU/storage, gateway/authorizer, logging/metrics, исходящий трафик, state/backup,
proxy и доля расходов существующих серверов ещё не оценены полностью. Free tier
других ресурсов аккаунта не проверен. Поэтому полная смета ещё не подтверждена.
Сценарий daily до 300 s + ещё 10 часов прочих jobs + указанные Lockbox/registry
даёт около 169 RUB до YDB/storage/egress/observability. Это запас для дальнейшей
оценки, не подтверждённая стоимость production.
M1 — ручные bounded probes без расписания;
бюджетные alerts не считаются hard cap, хранение/secret versions платные и в простое.

## Bootstrap, приёмка и незакрытые решения

### Публичный Terraform и приватные значения окружения

Terraform source содержит ресурсы, входные переменные, типы, проверки и безопасные
примеры без личных значений. Конкретный cloud, project folder, адрес proxy и
backend state передаются из GitHub Environment для выбранного окружения.

| Источник | Содержимое |
| --- | --- |
| Git: `.tf`, workflow, examples | Схема инфраструктуры, имена входов, placeholders; без персональных defaults |
| GitHub Environment Variables | Параметры, разрешённые к раскрытию: регион, лимиты ресурсов, публичный image digest |
| GitHub Environment Secrets | Приватные cloud/account/folder IDs и имена, proxy host/IP/URL, имя state bucket; учётные данные при необходимости |
| Private remote state | Созданные Terraform IDs/IPs и связи ресурсов; шифрование, ограниченный доступ, версии и locking |
| Lockbox | Runtime credentials; Terraform управляет контейнерами секретов и ссылками, не читает payload в state |

Workflow передаёт приватные значения через `TF_VAR_*` environment, а не подстановкой
в shell-код, command-line arguments или tracked `.tfvars`. Соответствующие inputs
и outputs помечаются `sensitive = true`. IDs/IPs, созданные ресурсами, берутся из
Terraform references/state, не переписываются вручную в исходники или GitHub Variables.
Нужные другому deployment приватные outputs передаются защищённым способом.

Backend использует partial configuration, загружаемую при init из приватного
временного файла; credentials передаются поддерживаемым backend механизмом env/identity.
Backend block не может читать обычные Terraform variables. `.terraform/`, реальные
tfvars, backend config, state и saved plans не попадают в Git. `.terraform.lock.hcl`
с версиями/checksums providers отслеживается в Git.

`sensitive` скрывает обычный вывод, **но не удаляет значения из state/plan**.
GitHub Secrets masking также не гарантирует сокрытие всех производных значений.
Raw plan/apply/show/output, JSON state/plan, debug traces и provider diagnostics
не публикуются в Actions logs, PR comments или общедоступные artifacts. Реальный
plan/apply пишет вывод в приватный файл/хранилище; наружу выводится только проверенная
сводка без resource IDs/IPs. Доступ к private plan/state и credentials предоставляется
лишь доверенному deployment workflow; PR из недоверенного кода выполняет только
проверки без реальных secrets/backend. Ограничения deployment environment и доступ
к приватным артефактам проверяются до первого настоящего plan в M1.

Это проектный контракт M1, не уже настроенные GitHub Environments или backend.
Источники: [Terraform sensitive data](https://developer.hashicorp.com/terraform/language/manage-sensitive-data),
[backend configuration](https://developer.hashicorp.com/terraform/language/backend),
[GitHub Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

### Готовность bootstrap и внешних условий

Предлагаемый IaC state: отдельный private versioned bucket в отдельном bootstrap
state, исключённом из destroy application stack, с блокировкой параллельного
apply и резервным восстановлением. Конкретные backend/lock mechanism проверить
Terraform validation и конкурентным тестом M1. Bootstrap identity отделена от
folder-scoped deploy SA; минимальные IAM права выводятся из выбранных ресурсов.
Registry — Yandex Container Registry; копировать immutable artifact без rebuild
и проверять digest. Secrets payload загружается вне Terraform state.

| Gate | Ответственный / срок | Текущее состояние и критерий |
| --- | --- | --- |
| Cloud, billing, folder | Исполнитель / M0–M1 | Cloud/billing read подтверждены; folder создаётся/import в M1; проверить effective bootstrap permissions |
| Полная смета | Исполнитель + владелец / до provisioning M1 | Оценить все компоненты и сверить с приватно согласованным бюджетом |
| Egress strength/runtime | Владелец + исполнитель / M0 | Выбрана прикладная маршрутизация через внешний proxy; интерфейс подключения требуется проверить |
| Cadence / разделение jobs | Владелец + исполнитель / M0 | Выбрано получение телеметрии при отчёте, без отдельного polling; полнота архива/config snapshots проверяется M2 |
| Внешний proxy | Владелец + исполнитель / M0, smoke M1 | Маршрут выбран; интерфейс, доступ, лимиты, журнал без payload, восстановление и стоимость требуют проверки |
| AI placement/account | Владелец + исполнитель / M0, smoke M2 | Фактическое размещение и схема доступа требуют проверки |
| State/bootstrap | Исполнитель / решение M0, проверка M1 | Предложен независимый private backend; параметры не provisioned |
| Web/TLS/DNS | Владелец + исполнитель / выбор M0, spike M1 | Нужен тестовый адрес и подтверждение управления DNS; auth transport не считать готовым |
| YDB semantics | Исполнитель / M3–M4 | Маппинг типов/операций выше; race, journal, graph size, fingerprint и import round-trip на Docker YDB, затем managed |

M0 считается завершённым после выбора внешних условий и проверки документов,
commit/push и явной приёмки владельцем. При неразрешённом M0 gate M1 не начинается;
legacy продолжает работать. Приёмка документа не является приёмкой реализации.

Rollback M8.1 предлагается держать не менее 14 последовательных дней нормальной
работы с проверкой дневного/недельного отчёта и репрезентативного длинного периода,
сохранности всех owner/AI данных, backup/restore и исключительности writer.
Длительность — предложение для приёмки M0. Перед откатом после cloud-записей
требуется обратный перенос/сверка delta, нельзя запускать старую SQLite и терять
новые данные. Только явное закрытие периода владельцем разрешает M8.2; старые
backup/image сохраняются отдельно, новый runtime после M8.2 использует только YDB.
