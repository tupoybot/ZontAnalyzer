# ZontAnalyzer — веха Cloud-first

Статус: план полной cloud-first переработки инфраструктуры<br>
Ветка: `architecture/cloud-first-serverless`<br>
База: `main`<br>
Архитектура: [`cloud_first_architecture.md`](./cloud_first_architecture.md)

Основной product/analytics функционал уже реализован в `main`. M0–M8 описывают отдельный инфраструктурный трек: IaC, cloud runtime, managed storage, публикацию, observability и cutover. Старая VPS/SQLite-инфраструктура сохраняется рабочей до завершения и приёмки миграции; функциональные изменения продукта в этот трек не входят и переносятся в `main` merge-ом или точечным `git cherry-pick`.

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
9. LLM usage ledger writes/reads.

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

---

## M5 — Publisher отчётов в Object Storage

### Цель

Публиковать отчёты без зависимости от persistent local filesystem.

### Работа

- Добавить publisher abstraction для rendered artifacts.
- Реализовать Object Storage publisher для:
  - стабильного `latest.html`;
  - датированного HTML;
  - канонического/переносимого JSON export, где это полезно.
- Канонический report state хранить в БД; Object Storage остаётся presentation/export target.
- Сделать публикацию идемпотентной и безопасной при retries.

### Критерии приёмки

- Завершённый analysis публикует валидный standalone HTML report в Object Storage.
- Повторный invocation не создаёт несогласованное состояние latest/archive.
- Output rendering соответствует semantics локального renderer.
- База данных не запускается с Object Storage mount.

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
- API Gateway для feedback/status endpoints;
- Postbox для weekly/важных email-сводок;
- Workflows/EventRouter, если orchestration станет понятнее, чем единый `run --once`;
- Message Queue, если понадобятся asynchronous retries/backpressure;
- Yandex Query + Object Storage для долгосрочного аналитического архива.

Cloud Functions разрешены для изолированных маленьких handlers, если они действительно проще расширения основного контейнера. Это не runtime по умолчанию.

### Критерии приёмки

- У каждого добавленного сервиса есть конкретная продуктовая или эксплуатационная причина.
- Ни один сервис не добавляется только ради более «cloud-native» вида архитектуры.
- Если Grafana включена, она читает только канонические данные через read-only доступ и не становится обязательной зависимостью ingestion/analysis/report pipeline.
- Feedback endpoints могут менять только состояние, принадлежащее ZontAnalyzer, но не ZONT/boiler controls.

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

### Критерии приёмки

- Нет необъяснимой потери/дублирования канонической telemetry или source events.
- Детерминированные факты отчёта совпадают в пределах ожидаемых различий algorithm/version.
- Существующая семантика DHW/reliability пережила миграцию.
- Cloud path переживает обычные retries/failures в течение согласованного периода наблюдения.
- Cutover имеет проверенный rollback path.

---

## Интеграция и приёмка через GitHub Issues/PR

Cloud-first является отдельным инфраструктурным треком и не должен дублировать product/analytics разработку.

Текущий статус ведётся в [GitHub Issue #1](https://github.com/tupoybot/ZontAnalyzer/issues/1). Issue #1 — родительский tracker для cloud-first направления; в нём хранятся активная веха, блокеры, ссылки на PR/коммиты и evidence. Для отдельной вехи можно открыть дочерний Issue, когда работа действительно начинается. Архитектура, устойчивые контракты и критерии приёмки остаются в этих versioned-документах.

Каждая веха оформляется отдельным GitHub Pull Request. В PR фиксируются scope, проверки, evidence, совместимость с legacy deployment и rollback/cutover impact. Веха считается принятой только после явного нажатия владельцем **Merge pull request** в интерфейсе GitHub. Текстовое подтверждение в чате, комментарий в Issue, зелёный CI, deployment или smoke не заменяют это действие.

```text
architecture/cloud-first-serverless
  -> полная cloud infrastructure: IaC / runtime / storage / publisher / observability
  -> legacy VPS/SQLite остаётся до cutover
  -> проверка и фиксация evidence
  -> PR и явный Merge pull request владельцем

product fixes
  -> отдельный PR в main или точечный cherry-pick
```

После merge PR Issue обновляется ссылкой на принятый PR и evidence, а следующий этап начинается от принятого состояния. Регулярный rebase выполняется только при необходимости обновить базу от `main`; product branches не обязаны ждать cloud-first работ.

## Definition of milestone complete

Полная cloud-first веха считается завершённой только после выполнения всех обязательных критериев: scheduled Serverless Container без постоянного VPS, каноническое состояние в выбранном managed backend, публикация отчётов в Object Storage, достаточная observability, сохранение read-only/safety/AI epistemic contracts и контролируемый cutover с legacy deployment. Каждая отдельная M0–M8 принимается через явный merge соответствующего GitHub PR.
