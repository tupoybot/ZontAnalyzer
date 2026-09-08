# ZontAnalyzer — веха Cloud-first

Статус: план полной cloud-first переработки инфраструктуры<br>
Ветка: `architecture/cloud-first-serverless`<br>
База: `main`<br>
Архитектура: [`cloud_first_architecture.md`](./cloud_first_architecture.md)

Сверено 2026-09-08: [текущий статус](./status.md) подтверждает приёмку до 8.5
и 9.1/9.2. M0–M8 ниже — план отдельного инфраструктурного трека, не свидетельство
его реализации и не замена продолжающегося этапа 9. Действующие источники
и сохраняемые контракты перечислены в [cloud-архитектуре](./cloud_first_architecture.md#статус-и-связь-с-действующими-документами).

Основной product/analytics функционал уже реализован в `main`. M0–M8 описывают отдельный инфраструктурный трек: IaC, cloud runtime, managed storage, публикацию, observability и cutover. Старая VPS/SQLite-инфраструктура сохраняется рабочей до завершения и приёмки миграции; функциональные изменения продукта в этот трек не входят и переносятся в `main` merge-ом или точечным `git cherry-pick`.

## Общие правила выполнения M0–M8

- Порядок зависимостей: M0 → M1 → ограниченный dev-spike M2 → решение M3 → M4
  → M5 → M6 → M8. M7 необязателен и не блокирует cutover. После M4 повторно
  проверить M2 на выбранном долговечном backend; dev-spike не доказывает production readiness.
- Сохранять актуальный baseline `main`, включая стоимость газа и сопровождение AI.
  Этапы 10/11, доставка и накопленная экономия не включаются в миграцию автоматически.
- Сборки, Python-проверки и подготовка данных — локально в Docker по
  [процедуре выпуска](./release-process.md). Копия production создаётся штатным
  SQLite online backup, скачивается и проверяется на отдельной записываемой копии
  с изолированной публикацией. Живые SQLite/WAL не копируются напрямую.
- HK остаётся deployment-only: проверенный immutable image и короткий smoke,
  без сборок, тестовых наборов, backfill и тяжёлой приёмки. Cloud-spikes выполняются
  в изолированных ресурсах с ограниченным объёмом и бюджетом; production не тестовый стенд.
- По умолчанию использовать `--no-ai`/моки; не более одного реального OpenAI-запроса
  на пользовательскую итерацию суммарно для всех исполнителей, включая metadata smoke.
- Для каждой вехи фиксировать проверки и ограничения. Документация требует только
  проверок документов/ссылок/diff и commit/push; развёртывание для неё неприменимо.

## M0 — Зафиксировать архитектурные контракты без изменения product baseline

### Цель

Определить границы, на которые будет опираться полная cloud-инфраструктура, не меняя стабильный product baseline в `main`.

### Работа

- Считать `run --once` основной единицей выполнения в облаке.
- Определить repository/storage interfaces, необходимые текущему SQLite-коду.
- Выявить SQLite-specific предположения в migrations, UPSERT, transactions, locking и lifecycle отчётов.
- Определить publisher abstraction для standalone report artifacts.
- Определить abstraction runtime-секретов/конфигурации, поддерживающую локальные files/env и managed cloud secrets.
- Сохранить текущую read-only границу ZONT неизменной.
- Составить карту переноса всех сохраняемых данных и существующих web/API операций,
  включая профиль, газ/тарифы, эксперименты, AI-настройки/пересмотр и аудит.

### Критерии приёмки

- Cloud-код можно разрабатывать без импорта Yandex-specific SDK в domain/analytics modules.
- Не появляется требований, блокирующих текущий runtime и отдельную разработку функциональных исправлений в `main`.
- Существующий SQLite/local runtime остаётся рабочим и зелёным.
- Архитектурное изменение, описанное в `cloud_first_architecture.md`, просмотрено и считается целевым для этой ветки.

---

## M1 — Terraform/IaC-скелет и пустой cloud runtime

### Цель

Создать облачный каркас проекта, пока не перенося application state.

### Работа

Развернуть через Terraform или выбранный в репозитории IaC-инструмент:

- service account(s) с least-privilege IAM;
- Container Registry / target для image;
- Serverless Container;
- Timer Trigger, напрямую вызывающий контейнер;
- placeholders/references секретов Lockbox;
- YDB Serverless database;
- Object Storage bucket/prefix для публикуемых отчётов;
- ресурсы Monium/monitoring, требующие явного provisioning.

Развернуть минимальную revision контейнера, которая стартует, подтверждает identity/config access, публикует тестовую health-метрику и успешно завершается.

Не добавлять Cloud Functions только ради вызова контейнера.

### Критерии приёмки

- Чистый cloud/folder воспроизводимо разворачивается из IaC.
- Timer по расписанию вызывает Serverless Container.
- Container работает под service account с ограниченными правами.
- Для скелета не нужен постоянно работающий VM/VPS.
- Секреты подключаются ссылками и не вшиваются в image/IaC source.
- Удаление тестового stack не затрагивает существующий production/pilot deployment.

---

## M2 — Запустить существующий `run --once` в Serverless Container с вынесенными за его пределы scratch/state boundaries

### Цель

Доказать, что существующее приложение способно работать в ephemeral container, пока ещё не принимая окончательно YDB в качестве основной БД.

### Работа

- Явно запускать one-shot application path при старте контейнера.
- Провести аудит использования filesystem: только временные файлы, кроме явно смонтированных/публикуемых данных.
- Отделить rendering отчёта от публикации в локальный path, чтобы позднее Object Storage стал отдельным publisher target.
- Добавить в логи invocation/job correlation IDs.
- Проверить адаптер invocation-протокола к CLI, ограниченный catch-up и продолжение
  задач по курсорам; фоновые задачи пересмотра моделей не должны теряться при выходе.
- Проверить timeout/retry behavior и безопасность повторных/параллельных invocation на границах приложения.
- Выполнить из реального Serverless Container smoke test исходящего HTTPS-доступа к `api.openai.com` **без API key**, чтобы отдельно доказать DNS/TLS/egress-доступность. До authenticated-вызова проверить, что фактический регион выполнения входит в официальный список регионов, где OpenAI API поддерживается. Если регион не поддерживается, не передавать туда рабочий API key и зафиксировать это как deployment blocker, а не обходить ограничение. Если регион поддерживается, выполнить один authenticated запрос к metadata endpoint модели с ожидаемым `200`; generation для smoke test не нужен.

На этом этапе допустим временный/dev backend состояния; нельзя выдавать ephemeral local SQLite за production-safe решение.

### Критерии приёмки

- Реальный application image запускается и завершает ограниченный по времени one-shot run в Serverless Containers.
- Корректность долговечного состояния не зависит от сохранения локальной filesystem контейнера между invocation.
- Два перекрывающихся invocation не приводят к неконтролируемым дублирующим side effects.
- Failure виден в logs/metrics, а следующий invocation может безопасно повторить работу.
- Для OpenAI отдельно зафиксированы сетевой результат и региональная совместимость; API key не используется из неподдерживаемого OpenAI региона.

---

## M3 — Spike YDB storage

### Цель

На основании фактов решить, должна ли YDB стать production backend состояния.

### Обязательные representative operations

Реализовать или прототипировать достаточную часть YDB adapter, чтобы проверить:

1. telemetry sample UPSERT/idempotency;
2. time-range query samples для одного/нескольких series;
3. entity/series discovery queries;
4. persistence/query source events;
5. save/load report;
6. lifecycle recommendation feedback/intervention;
7. updates sync cursor;
8. atomic job claim/lease/idempotency;
9. LLM usage ledger writes/reads;
10. профиль, показания/калибровка газа, месячные тарифы/валюты и аудит;
11. версии AI-настроек, предложения/решения пересмотра, provenance и кэши;
12. периоды/ревизии и согласованное чтение данных для расчёта/публикации.

Тестировать на sanitized subset/model реальной установки и реалистичном объёме telemetry.

### Критерии приёмки

- Для каждой обязательной операции есть документированная реализация YDB или явно выявленный blocker.
- Transaction/concurrency semantics для job claiming доказаны тестами, а не предположены.
- Query shape и indexes приемлемы для daily/weekly analytical workloads.
- Оценки cost/storage записаны по реальным измерениям или platform calculators, а не придуманы.
- Если YDB отклоняется, до дальнейшей миграции документируются причина и альтернативный managed backend.

---

## M4 — Два storage adapter и contract tests

### Цель

Сделать storage заменяемым, не размазывая детали YDB по приложению.

### Работа

- Ввести/доделать repository contracts там, где это требуется.
- Сохранить SQLite adapter для local/test mode.
- Реализовать YDB adapter для cloud mode.
- Запускать один и тот же storage contract test suite для обоих backend там, где семантика должна совпадать.
- Заменить Alembic-only предположения на backend-aware schema management, сохраняя безопасную migration discipline.

### Критерии приёмки

- Application/analytics code для обычных операций не ветвится по `sqlite`/`ydb`.
- Core storage contract suite проходит на обоих backend.
- Существующие SQLite tests остаются зелёными.
- YDB migrations/schema initialization воспроизводимы и идемпотентны.
- Сохранены идентичности, связи, исходная гранулярность, пользовательские данные,
  аудит, UTC-время и календарные границы в зоне объекта; настройки не переносятся назад.
- Одинаковые входные данные периода не вызывают пересчёта из-за смены backend;
  кэши, исходный отпечаток фактов AI и неизвестная историческая модель сохраняются.
- Конкурентные записи настроек/feedback, истечение lease и неизвестный результат
  внешнего AI-вызова проверены; слепой повтор генерации не используется для восстановления.

---

## M5 — Публикация в Object Storage и сохранение защищённого web/API

### Цель

Публиковать отчёты без зависимости от persistent local filesystem.

### Работа

- Добавить publisher abstraction для rendered artifacts.
- Реализовать Object Storage publisher для:
  - стабильного `latest.html` только для дневного отчёта;
  - датированного HTML;
  - архива дневных, недельных, месячных и сезонных отчётов и `reports.json`;
  - канонического/переносимого JSON export, где это полезно.
- Канонический report state хранить в БД; Object Storage остаётся presentation/export target.
- Сделать публикацию идемпотентной и безопасной при retries.
- Перенести существующие web/API операции: комментарии/feedback, эксперименты,
  профиль, показания, тарифы и AI-настройки. API Gateway — кандидат, выбор сервиса
  не меняет обязательность сохранения этих функций до cutover.
- Сохранить общий периметр HTML/JSON/manifest/API, закрыть обход через bucket.

### Критерии приёмки

- Завершённый analysis публикует валидный standalone HTML report в Object Storage.
- Повторный invocation не создаёт несогласованное состояние latest/archive.
- Output rendering соответствует semantics локального renderer.
- База данных не запускается с Object Storage mount.
- Файлы готовы до manifest, latest обновляется после архива; сбой и устаревший
  конкурентный publisher не создают ссылки на незавершённые объекты и не откатывают новую публикацию.
- Сохранение пользовательских данных предшествует публикации; просмотр/рендер
  не вызывают AI и не подменяют его ответ. Переносимый HTML экранирует текст.
- Браузерные проверки desktop/mobile подтверждают навигацию по типам/архиву,
  формы, стоимость и происхождение AI; неавторизованный доступ закрыт.

---

## M6 — Cloud observability и ограничения стоимости

### Цель

Сделать pet project достаточно наблюдаемым, чтобы его можно было оставить работать без постоянного просмотра cloud console.

### Работа

Собирать/экспортировать как минимум:

- sync success/failure и lag;
- возраст последней telemetry;
- analysis/report duration;
- container invocation failure/timeout;
- OpenAI call count/tokens/failures;
- состояние и сроки пересмотра моделей, ограниченные повторы, предложения и решения;
- application reliability metrics, уже вычисляемые по ZONT events;
- YDB/storage errors;
- report publication failures.

Добавить практические billing/cost guardrails и задокументировать ожидаемое поведение в idle/normal режиме.

### Критерии приёмки

- Failed sync и stale telemetry различимы от успешного выполнения контейнера.
- Как минимум один alert path проверен end-to-end.
- Неожиданные invocation loops или скачки token usage наблюдаемы.
- Monthly cost guardrails/alerts настроены или явно задокументированы.

---

## M7 — Опциональные cloud UX services и визуализация

### Цель

Добавлять managed services только там, где они упрощают реальный use case, и при желании дать проекту удобный инженерный UI для исследования телеметрии.

Возможные дополнения:

- **Grafana Cloud Free** как опциональный visualization layer для красивых инженерных графиков из канонической telemetry: комнаты/уставки, улица, подача/обратка/ΔT, ГВС, горелка, reliability events и interventions. Это не source of truth и не обязательная часть ZontAnalyzer: штатные графики ZONT и standalone HTML остаются достаточными для обычного использования. Если Grafana подключается напрямую к YDB, использовать отдельные строго read-only credentials/service identity;
- дополнительные API endpoints сверх обязательного переноса существующих операций в M5;
- Postbox как кандидат для email после отдельного выбора задачи по [плану доставки](./delivery-plan.md);
- Workflows/EventRouter, если orchestration станет понятнее, чем единый `run --once`;
- Message Queue, если понадобятся asynchronous retries/backpressure;
- Yandex Query + Object Storage для долгосрочного аналитического архива.

Cloud Functions разрешены для изолированных маленьких handlers, если они действительно проще расширения основного контейнера. Это не runtime по умолчанию.

### Критерии приёмки

- У каждого добавленного сервиса есть конкретная продуктовая или эксплуатационная причина.
- Ни один сервис не добавляется только ради более «cloud-native» вида архитектуры.
- Если Grafana включена, она читает только канонические данные через read-only доступ и не становится обязательной зависимостью ingestion/analysis/report pipeline.
- Feedback endpoints могут менять только состояние, принадлежащее ZontAnalyzer, но не ZONT/boiler controls.
- Telegram/email остаются отложенными: доставка использует сохранённый отчёт,
  не запускает AI, не обходит авторизацию и не блокирует ingestion/analysis/publication.
  При выборе канала отдельно проверяются повторы, неизвестный исход и отключение.

---

## M8 — Контролируемый cutover и интеграция в `main`

### Цель

Перенести production/pilot state после того, как cloud path достаточно убедительно доказал эквивалентность. До завершения этой вехи legacy VPS/SQLite deployment остаётся рабочим rollback path; после успешного cutover инфраструктурный результат вливается в `main` через принятый PR.

### Работа

- Export/import или backfill канонической telemetry/state в YDB.
- Параллельно, но временно сравнивать legacy SQLite/VPS и cloud path до cutover.
- Сравнивать sync coverage, факты отчётов, DHW/reliability outputs и содержимое AI packet.
- Проверить failure/recovery behavior: network loss, ошибки ZONT API, перекрывающиеся timer invocations, OpenAI failure, transient storage errors.
- Определить процедуры rollback и финального cutover.
- Мигрировать полный состав состояния из M3/M4, включая пользовательские записи,
  тарифы/валюты, версии AI, provenance и журнал. Сравнение выполняется локально
  на изолированной копии; облачный pilot использует отдельные ресурсы и публикацию.
- На время финального переноса определить единственного писателя, остановку старого
  расписания и пользовательских записей, перенос финальной дельты и проверку итогов.
- До переключения согласовать с владельцем план; определить возврат новых cloud-записей
  при rollback. Старая SQLite-копия сама по себе не сохраняет изменения после cutover.

### Критерии приёмки

- Нет необъяснимой потери/дублирования канонической telemetry или source events.
- Детерминированные факты отчёта совпадают в пределах ожидаемых различий algorithm/version.
- Существующая семантика DHW/reliability пережила миграцию.
- Cloud path переживает обычные retries/failures в течение согласованного периода наблюдения.
- Cutover имеет проверенный rollback path.
- Сохранены исходные измерения и все пользовательские данные/аудит, тарифы,
  настройки и AI provenance; миграция не запускает массовую повторную генерацию.
- Работают защищённые страницы, архив и все формы M5; после переключения нет
  двух активных расписаний/писателей. Rollback проверяет и данные, и маршрутизацию.

---

## Интеграция и приёмка через GitHub Issues/PR

Cloud-first является отдельным инфраструктурным треком и не должен дублировать product/analytics разработку.

Текущий статус ведётся в [GitHub Issue #1](https://github.com/tupoybot/ZontAnalyzer/issues/1). Issue #1 — родительский tracker для cloud-first направления; в нём хранятся активная веха, блокеры, ссылки на PR/коммиты и evidence. Для отдельной вехи можно открыть дочерний Issue, когда работа действительно начинается. Архитектура, устойчивые контракты и критерии приёмки остаются в этих versioned-документах.

Каждая веха оформляется отдельным GitHub Pull Request. В PR фиксируются scope,
проверки, evidence, совместимость с legacy deployment и rollback/cutover impact.
Реализация, проверки, commit/push, применимое развёртывание и bounded smoke
выполняются до запроса приёмки. Merge разрешён только после явного принятия
владельцем по [действующей процедуре](./release-process.md), в том числе в чате;
кнопка GitHub не является единственным способом приёмки. CI, deployment, smoke
и молчание её не заменяют. Перед merge — локальный `docker system prune -af`, не на HK.
Документационные изменения идут в `docs/<topic>`, продуктовые — в `stage9/<topic>`.

```text
architecture/cloud-first-serverless
  -> полная cloud infrastructure: IaC / runtime / storage / publisher / observability
  -> legacy VPS/SQLite остаётся до cutover
  -> проверка и фиксация evidence
  -> PR, явная приёмка владельца, затем merge

product fixes
  -> отдельный PR в main или точечный cherry-pick
```

После merge PR Issue обновляется ссылкой на принятый PR и evidence, а следующий этап начинается от принятого состояния. Регулярный rebase выполняется только при необходимости обновить базу от `main`; product branches не обязаны ждать cloud-first работ.

## Definition of milestone complete

Полный cloud-first трек завершается после обязательных M0–M6 и M8: scheduled
Serverless Container без постоянного VPS, каноническое состояние в проверенном
managed backend, защищённые отчёты в Object Storage и существующий web/API,
observability, сохранение продуктовых контрактов и контролируемый cutover/rollback.
M7 выполняется только по отдельной потребности. Каждая выбранная веха требует
технических доказательств, явной приёмки владельца и последующего merge её PR.
