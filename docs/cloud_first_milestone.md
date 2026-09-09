# ZontAnalyzer — веха Cloud-first

Статус: план полной cloud-first переработки инфраструктуры<br>
Ветка: `architecture/cloud-first-serverless`<br>
База: `main`<br>
Архитектура: [`cloud_first_architecture.md`](./cloud_first_architecture.md)

Сверено 2026-09-08: [текущий статус](./status.md) подтверждает приёмку до 8.5
и 9.1/9.2. M0–M8 ниже — план отдельного инфраструктурного трека, не свидетельство
его реализации и не замена продолжающегося этапа 9. Действующие источники
и сохраняемые контракты перечислены в [cloud-архитектуре](./cloud_first_architecture.md#статус-и-связь-с-действующими-документами).

Основной product/analytics функционал уже реализован в `main`. M0–M8 описывают отдельный инфраструктурный трек: IaC, cloud runtime, managed storage, публикацию, observability и cutover. Старая VPS/SQLite-инфраструктура сохраняется рабочей до завершения и приёмки миграции; функциональные изменения продукта в этот трек не входят и проходят отдельный review/merge в `main`.

## Общие правила выполнения M0–M8

- Порядок зависимостей: M0 (контракты и внешняя реализуемость) → M1 (каркас,
  egress и минимальные лимиты) → M2 (ограниченный dev-spike) → M3 (выбор БД)
  → M4 (адаптеры и повторная приёмка M2) → M5 → M6 → M8.
  M7 необязателен и не блокирует cutover. Минимальные логи, ограничения расходов
  и выключение расписания нужны с M1; M6 завершает эксплуатационную наблюдаемость.
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

## CI/CD и единый immutable application artifact

CI/CD разделяет выпуск приложения и развитие cloud-инфраструктуры. Функциональная
разработка приложения идёт в feature-ветках и попадает в `main` через обычные
проверенные PR. Каждый application release собирается из принятого `main`, проходит
локальные проверки и публикуется как immutable image с digest. Cloud-вещи могут
развиваться отдельной инфраструктурной веткой и не требуют переноса каждого её
коммита в `main`.

Инфраструктурный PR выбирает уже проверенный digest application image; он не
собирает собственную копию приложения и не подменяет baseline кодом, который
существует только в cloud-ветке. Один и тот же digest должен быть пригоден для
одновременного запуска на VPS и в cloud. Различия окружений задаются отдельной
конфигурацией и managed secrets, а не изменением image layers: runtime target,
storage/publisher, расписание, credentials, egress policy и включённость side
effects принадлежат environment configuration.

До M8 такая одновременность означает только контролируемый shadow/pilot режим.
Cloud получает изолированные state, publication prefix и side-effect sinks;
production analysis, AI generation, messages, feedback writes и другие внешние
эффекты не дублируются. Допустимы bounded read-only сравнения и mock/no-op
side effects. Единственный production writer и расписание остаются на legacy VPS
до согласованного cutover; после него writer переносится по процедуре M8.

Если cloud runtime требует совместимости, соответствующий код приложения сначала
проходит отдельный review и merge в `main`, после чего новый digest выпускается
из `main`. Нельзя считать код из инфраструктурной ветки частью application image
или выдавать cloud-only изменения за проверенный VPS/cloud baseline.

Эти правила применяются с M1 (registry, image и environment wiring), проверяются
на ограниченном shadow/pilot в M2–M6 и входят в критерии M8. Они не изменяют
порядок зависимостей M0–M8 и не разрешают перенос production state до доказанного
cutover.

## Внешние зависимости и условия продолжения

План не доказывает доступность конкретного аккаунта, региона или сетевого маршрута.
В M0 для каждой строки записать предварительное решение, ответственного и срок
проверки; фактические результаты добавить на указанной вехе. Неизвестное не считать
успешной проверкой. Внешний blocker закрывается доступной
альтернативой или останавливает зависимую работу, а не переносится к cutover.

| Зависимость | Когда закрыть | Проверка и выход при недоступности |
| --- | --- | --- |
| Cloud account, billing, IAM, квоты, IaC state и registry | M0; provisioning в M1 | Проверить доступ исполнителя и бюджет; определить bootstrap identity и защищённое хранилище IaC state вне удаляемого stack. При отсутствии доступа M1 не начинать. |
| OpenAI: поддерживаемая география, аккаунт и модели baseline | Выбор размещения в M0; сетевой/auth smoke в M2 | Проверить допустимость всей схемы по официальным условиям. Если несовместима, выбрать поддерживаемое размещение AI/runtime; при отсутствии приемлемого варианта остановить AI-миграцию, сохранить legacy. `--no-ai` пригоден для разработки, но не заменяет функциональную приёмку. |
| Управляемый исходящий proxy/egress | Контракт в M0; стенд и проверка в M1 | Зафиксировать провайдера/размещение, доступ, стоимость, владельца и восстановление. Ни HK/Xray, ни личный proxy разработчика не являются неявной production-зависимостью. Если выбранный runtime не позволяет нужный контроль, пересмотреть runtime/сетевую схему до M2. |
| ZONT API и облачные endpoints | Маршруты в M1; bounded read-only smoke в M2 | Проверить DNS/TLS, авторизацию и лимиты из выбранного runtime. Моки разрешают разработку, но не закрывают приёмку интеграции. |
| Managed storage | M3 до M4 | YDB — кандидат; при отказе выбрать и проверить другой managed backend на тех же операциях, затем обновить IaC и архитектуру. |
| Защищённый web/API, домен и TLS | Выбор и технический spike в M1; полная проверка в M5 | Проверить приватный bucket, передачу identity/заголовков и доступ к DNS/сертификатам. До M8 использовать отдельный тестовый адрес; без работоспособного периметра cutover запрещён. |

Опорные документы проверены 2026-09-08: [OpenAI — поддерживаемые страны](https://developers.openai.com/api/docs/supported-countries),
[сеть Serverless Containers](https://yandex.cloud/ru/docs/serverless-containers/concepts/networking),
[режимы контейнера и registry](https://yandex.cloud/en/docs/serverless-containers/concepts/container),
[вызовы и фильтрация заголовков](https://yandex.cloud/en/docs/serverless-containers/concepts/invoke).
Перед реализацией повторно сверить ограничения выбранного режима и региона.

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
- Зафиксировать границы application artifact, environment configuration и
  shadow/pilot state для последующих CI/CD и dual-runtime проверок.
- Закрыть предварительные решения из таблицы внешних зависимостей, включая
  размещение AI и механизм контроля исходящего трафика по контракту ниже.
- Выбрать invocation-протокол и записать бюджет cold start, работы, сетевых
  таймаутов и retries; большие задачи должны делиться на возобновляемые порции.

### Контракт проксирования и контроля трафика

- Ввести явную политику маршрутов по назначениям: ZONT, OpenAI generation/metadata,
  каталог моделей, cloud APIs и служебная identity. Для каждого маршрута выбрать
  `proxy`, разрешённый `direct` или `deny`; незаданные внешние назначения запрещены.
  Это будущий контракт конфигурации, а не уже существующие поля приложения.
- Базовый механизм для внешнего HTTPS — аутентифицированный forward proxy с CONNECT,
  allowlist назначений/портов, лимитами соединений/байтов и таймаутами. TLS к целевому
  API проверяет клиент; перехват/расшифровка TLS не требуется. Соединение до proxy
  также защищено TLS либо приватным защищённым транспортом.
- Все HTTP-клиенты должны применять одну политику, включая SDK OpenAI и каталог
  моделей. Не полагаться только на глобальный `HTTPS_PROXY`: проверить влияние
  `NO_PROXY`, redirects, DNS и переменных окружения на каждом адаптере. Служебный
  metadata/identity endpoint и cloud SDK используют отдельно разрешённые маршруты.
- При обязательном proxy его отказ блокирует соответствующий внешний вызов без
  автоматического direct fallback. На уровне сети/runtime запретить прямой обход
  для этих назначений и проверить попыткой прямого соединения. Настройка клиента
  сама по себе не доказывает сетевую изоляцию; подключение VPC само по себе тоже.
- CONNECT позволяет контролировать назначения и объём соединений, но не видеть
  HTTPS path, число API-запросов или токены. Лимиты запросов/токенов/расходов
  реализуются в приложении с общим долговечным учётом после M4.
- Proxy credentials хранить через secrets abstraction/Lockbox. В журналы писать
  назначение, разрешение/отказ, длительность и байты; не писать ключи, заголовки
  авторизации, тела запросов и телеметрию. Определить срок хранения журналов.
- Выбрать воспроизводимый proxy service с независимым от HK lifecycle. Если
  потребуется собственная постоянная VM, явно пересмотреть цель «без постоянного
  VPS» и стоимость до реализации; не прятать её за термином serverless.
- Прокси контролирует трафик, но не подтверждает региональную совместимость OpenAI.
  Смена выходного IP сама по себе не закрывает проверку допустимости размещения.

### Критерии приёмки

- Cloud-код можно разрабатывать без импорта Yandex-specific SDK в domain/analytics modules.
- Не появляется требований, блокирующих текущий runtime и отдельную разработку функциональных исправлений в `main`.
- Существующий SQLite/local runtime остаётся рабочим и зелёным.
- Архитектурное изменение, описанное в `cloud_first_architecture.md`, просмотрено и считается целевым для этой ветки.
- Для зависимостей со сроком M0 есть решение и доказательства доступности;
  для проверок M1–M3 указаны критерий, ответственный и альтернатива при отказе.
  Размещение AI и способ контроля egress выбраны до создания основного stack.

---

## M1 — Terraform/IaC-скелет и пустой cloud runtime

### Цель

Создать облачный каркас проекта, пока не перенося application state.

### Работа

Развернуть через Terraform или выбранный в репозитории IaC-инструмент:

- service account(s) с least-privilege IAM;
- Yandex Container Registry как target для локально проверенного immutable image;
- environment-specific configuration и references на secrets отдельно от image;
- механизм выбора конкретного application image digest из `main` для VPS и cloud;
- Serverless Container;
- Timer Trigger, напрямую вызывающий контейнер; включить после ручного smoke;
- placeholders/references секретов Lockbox;
- изолированную YDB Serverless database для M3, ещё не production backend;
- Object Storage bucket/prefix для публикуемых отчётов;
- ресурсы Monium/monitoring, требующие явного provisioning;
- выбранный proxy/egress, необходимые сетевые ресурсы и secret references;
- минимальные логи/alerts, бюджет стенда, ограничения invocation/concurrency/retries
  и документированное выключение расписания. Billing alert не считать hard cap.

Развернуть минимальную revision контейнера, которая обрабатывает вызов выбранного
режима, подтверждает identity/config access, публикует тестовую health-метрику
и возвращает успешный результат. Для HTTP server mode нужен обработчик на `PORT`;
завершение CLI при старте само по себе не является обработкой timer invocation.

На синтетическом endpoint проверить разрешённый proxy-маршрут, запрет постороннего
назначения, отсутствие прямого обхода и отказ proxy. Отдельным маленьким web-spike
проверить приватный объект и защищённый API через выбранный ingress: нельзя
предполагать, что Basic Auth/cookies проходят фильтрацию заголовков без изменений.

Не добавлять Cloud Functions только ради вызова контейнера.

### CI/CD actions for M1

- Application-release workflow принимает только `main`: для одного commit SHA
  выполняет проверки, собирает image ровно один раз, публикует его по digest и
  сохраняет provenance (commit SHA, digest, workflow run и результаты проверок).
- Infrastructure-deploy workflow принимает digest как явный immutable input из
  registry; он планирует/разворачивает IaC и environment configuration в cloud,
  не пересобирая application image. Выбор digest и environment config видны в
  deployment evidence.
- Workflow и IaC поддерживают dual-target deployment одного digest на VPS и в
  cloud с раздельными secrets/configuration; M1 проверяет выбор digest,
  rendering и bounded initial runtime smoke. Фактическая одновременная проверка
  running image digests относится к повторной приёмке M2 в M4 и финальной проверке
  M8, после доказательства runtime/storage compatibility.
- Cloud-only code не попадает в application image. Если нужен runtime-compatibility
  change, отдельный PR сначала принимает его в `main`, затем release workflow
  выпускает новый digest, который выбирает infrastructure workflow.

### Критерии CI/CD-приёмки

- По одному `main` commit SHA существует один опубликованный immutable digest и
  воспроизводимая provenance-запись; deployment не принимает floating tag.
- Cloud deployment изменяет только выбранные infrastructure/environment targets;
  M1 подтверждает digest selection/rendering и bounded smoke, не требуя готового
  dual-runtime production path.
- Конфигурация, secrets, state, publication namespace и side-effect sinks
  разделены по окружениям; M1 smoke не запускает duplicate production analysis,
  AI generation, messages или пользовательские записи.
- Deployment может быть повторён с тем же digest без пересборки и без изменения
  application artifact; legacy VPS остаётся рабочим rollback path.

### Критерии приёмки

- Чистый cloud/folder воспроизводимо разворачивается из IaC.
- Timer по расписанию вызывает Serverless Container.
- Container работает под service account с ограниченными правами.
- Для скелета не нужен постоянно работающий VM/VPS.
- Секреты подключаются ссылками и не вшиваются в image/IaC source.
- Удаление тестового stack не затрагивает существующий production/pilot deployment.
- Proxy/egress проверки пройдены из реального runtime; доступ к identity, secrets
  и storage работает по явно разрешённым маршрутам. Постоянные сетевые расходы учтены.
- Выбран реализуемый web/API-периметр; лимиты и отключение timer проверены до M2.

---

## M2 — Запустить существующий `run --once` в Serverless Container с вынесенными за его пределы scratch/state boundaries

### Цель

Доказать, что существующее приложение способно работать в ephemeral container, пока ещё не принимая окончательно YDB в качестве основной БД.

### Работа

- Запускать one-shot application path на каждый invocation через адаптер выбранного
  режима; повторный вызов прогретого экземпляра тоже обрабатывается.
- Провести аудит использования filesystem: только временные файлы, кроме явно смонтированных/публикуемых данных.
- Отделить rendering отчёта от публикации в локальный path, чтобы позднее Object Storage стал отдельным publisher target.
- Добавить в логи invocation/job correlation IDs.
- Проверить адаптер invocation-протокола к CLI, ограниченный catch-up и продолжение
  задач по курсорам; фоновые задачи пересмотра моделей не должны теряться при выходе.
- Проверить timeout/retry behavior и безопасность повторных/параллельных invocation на границах приложения.
- Из реального runtime по маршрутам M0/M1 проверить DNS/TLS/egress к ZONT и OpenAI,
  сначала без ключей. После подтверждения региональной совместимости из M0 выполнить
  не более одного authenticated metadata-запроса OpenAI с ожидаемым `200`; generation
  для сетевого smoke не нужен. Для ZONT — ограниченный read-only запрос. Отдельно
  проверить отказ proxy; реальные API не использовать для нагрузочных/failure тестов.

На этом этапе допустим временный/dev backend состояния и изолированные fixtures;
нельзя выдавать ephemeral local SQLite за production-safe решение. Проверки ниже
касаются invocation и ограниченного сценария с моками side effects. Доказательство
durability, leases и безопасности реальных конкурентных записей относится к M4.

### Критерии приёмки

- Реальный application image запускается и завершает ограниченный по времени one-shot run в Serverless Containers.
- Уничтожение scratch не скрывает зависимость от состояния: все нужные долговечные
  записи перечислены для M3/M4; fixture-сценарий воспроизводится в новом экземпляре.
- Перекрывающиеся invocation проверены с mock side effects; production schedule,
  пользовательские записи и конкурентная генерация до M4 не включаются.
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
- Реализовать adapter выбранного в M3 managed backend для cloud mode (YDB при принятии).
- Запускать один и тот же storage contract test suite для обоих backend там, где семантика должна совпадать.
- Заменить Alembic-only предположения на backend-aware schema management, сохраняя безопасную migration discipline.

### Критерии приёмки

- Application/analytics code для обычных операций не ветвится по `sqlite`/`ydb`.
- Core storage contract suite проходит на обоих backend.
- Существующие SQLite tests остаются зелёными.
- Migrations/schema initialization выбранного backend воспроизводимы и идемпотентны.
- Повторная приёмка M2 выполняется на выбранном application digest после
  доказательства storage compatibility: VPS и cloud запускаются на одном digest
  с раздельными environment configuration, shadow state и side-effect sinks;
  фактические running digests фиксируются. Production analysis, AI, messages и
  пользовательские записи не дублируются. Финальная повторная проверка этого
  dual-runtime контракта и single-writer перехода выполняется в M8.
- Сохранены идентичности, связи, исходная гранулярность, пользовательские данные,
  аудит, UTC-время и календарные границы в зоне объекта; настройки не переносятся назад.
- Одинаковые входные данные периода не вызывают пересчёта из-за смены backend;
  кэши, исходный отпечаток фактов AI и неизвестная историческая модель сохраняются.
- Конкурентные записи настроек/feedback, истечение lease и неизвестный результат
  внешнего AI-вызова проверены; слепой повтор генерации не используется для восстановления.
- Повторная приёмка M2 проходит на долговечном backend: fresh/warm invocation,
  потеря scratch, обрыв и продолжение catch-up, перекрытие задач и общие лимиты
  внешних вызовов. До M5 эти проверки обязательны; одного dev-spike недостаточно.
- Инструменты полного export/import и обратного переноса изменений проверены
  локально на изолированной копии; M8 использует готовые инструменты миграции.

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
- report publication failures;
- proxy availability/denials, объём трафика по назначениям, срабатывания лимитов
  и запреты прямого обхода; отличать отказ proxy от ошибки внешнего API.

Завершить начатые в M1 billing/cost guardrails и задокументировать ожидаемое поведение
в idle/normal режиме, включая стоимость proxy, egress и хранения журналов.

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

- Выполнить отрепетированный в M4 export/import канонической telemetry/state
  в выбранный managed backend; backfill только для выявленных пробелов.
- Параллельно, но временно сравнивать legacy SQLite/VPS и cloud path до cutover.
- Сравнивать sync coverage, факты отчётов, DHW/reliability outputs и содержимое AI packet.
- Проверить failure/recovery behavior: network loss, ошибки ZONT API, перекрывающиеся timer invocations, OpenAI failure, transient storage errors.
- Подтвердить готовность M0–M6, доступ к DNS/TLS и отсутствие зависимости от HK
  для cloud egress; проверить отказ/восстановление proxy и сохранение его политики.
- Определить процедуры rollback и финального cutover.
- Мигрировать полный состав состояния из M3/M4, включая пользовательские записи,
  тарифы/валюты, версии AI, provenance и журнал. Сравнение выполняется локально
  на изолированной копии; облачный pilot использует отдельные ресурсы и публикацию.
- На время финального переноса определить единственного писателя, остановку старого
  расписания и пользовательских записей, перенос финальной дельты и проверку итогов.
- Убедиться, что параллельный период использовал один и тот же application image
  digest на VPS и cloud, раздельную конфигурацию и изолированные shadow state,
  публикацию и side-effect sinks; production analysis/AI/messages не дублировались.
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
PR вех M0–M7 и документации этого трека направляются в
`architecture/cloud-first-serverless`; промежуточная приёмка не переносит трек
в `main`. Итоговый PR из архитектурной ветки в `main` относится к M8 и требует
отдельной явной приёмки. Это сохраняет независимость текущего product baseline.

```text
architecture/cloud-first-serverless
  -> полная cloud infrastructure: IaC / runtime / storage / publisher / observability
  -> legacy VPS/SQLite остаётся до cutover
  -> проверка и фиксация evidence
  -> PR, явная приёмка владельца, затем merge

product fixes
  -> отдельный review/PR в main
```

После merge PR Issue обновляется ссылкой на принятый PR и evidence, а следующий этап начинается от принятого состояния. Регулярный rebase выполняется только при необходимости обновить базу от `main`; product branches не обязаны ждать cloud-first работ.

## Definition of milestone complete

Полный cloud-first трек завершается после обязательных M0–M6 и M8: scheduled
Serverless Container без постоянного VPS, каноническое состояние в проверенном
managed backend, защищённые отчёты в Object Storage и существующий web/API,
observability, сохранение продуктовых контрактов и контролируемый cutover/rollback.
M7 выполняется только по отдельной потребности. Каждая выбранная веха требует
технических доказательств, явной приёмки владельца и последующего merge её PR.
