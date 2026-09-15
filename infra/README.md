# Первоначальная установка M1

Текущее состояние и ограничения: [M1](../docs/cloud_first_m1.md).
Все команды ниже выполняются локально. Python, Terraform и сборки запускаются
в Docker; credentials, inputs, планы и outputs хранятся в приватном каталоге.

## 1. Первичная инфраструктура

Root `bootstrap/` создаёт каталог в явно разрешённом облаке, служебные учётные
записи и backend состояния. Подготовить приватный `inputs.tfvars.json` по
обязательным переменным `bootstrap/variables.tf`. Проверить `cloud_id` до первого
запроса. В отдельный рабочий каталог скопировать `.tf` и `.terraform.lock.hcl`.
Для GitHub задать также числовые `github_repository_owner_id` и
`github_repository_id` из API репозитория. Федерация использует неизменяемый
формат subject с этими ID и точным именем защищённого Environment. Для старого
репозитория предварительно включить immutable subject в настройках GitHub;
новые репозитории используют его по умолчанию с июля 2026 года.

Собрать инструменты: `docker build -t zont-m1-tools:local infra/tools`.
В контейнере с приватной конфигурацией и bootstrap IAM-токеном выполнить
`terraform init`, `terraform plan -var-file=inputs.tfvars.json -out=bootstrap.tfplan`,
проверить приватный план и применить его. Начальное состояние хранится локально
с правами только для владельца. `prevent_destroy` защищает каталог, bucket и ключ.

Создать статический ключ доступа deployment-аккаунта вне Terraform, сохранить
его приватно. Создать приватный backend по `backend.example.hcl`, добавить
`backend "s3" {}` в рабочую копию bootstrap root и выполнить
`terraform init -migrate-state -backend-config=bootstrap.tfbackend.json`.
Проверить версии и шифрование загруженного состояния. Сохранить защищённую
резервную копию начального локального состояния.

## 2. Приватная конфигурация облачного окружения

Подготовить каталог с правами `0700`, файлы — `0600`:

- `scope.json`: `allowed_cloud_id`, `allowed_folder_id`, `allowed_state_bucket`,
  `allowed_deployment_service_account_id`; в `allowed_environment_accounts`
  карты `dev`/`pilot` с `runtime_service_account_id` и `timer_service_account_id`.
  Значения берутся из проверенных bootstrap outputs, а не из активного профиля CLI.
- `cloud.tfbackend.json`: выбранный bucket, endpoint Object Storage,
  `key` равен `dev/terraform.tfstate` или `pilot/terraform.tfstate`,
  `use_lockfile=true`. Остальные параметры — из примера backend.
- `state.env`: `AWS_ACCESS_KEY_ID` и `AWS_SECRET_ACCESS_KEY` для backend.
- `deploy-token`: IAM-токен deployment-аккаунта.
- `cloud-work/inputs.tfvars.json`: значения переменных `cloud/variables.tf`.
  Изначально `enable_timer=false`, `attach_domain=false`. Для подготовки registry
  разрешена временная синтаксически корректная ссылка `probe_image` с digest;
  она не применяется к runtime. До полного apply заменить её реальным образом.

Сначала выполнить `infra/deploy.sh plan-foundation PRIVATE_DIRECTORY`.
Проверить, что план создаёт только registry и пустой Lockbox. Затем выполнить
`infra/deploy.sh apply-foundation PRIVATE_DIRECTORY`. Сценарий откажется применять
этим режимом план с изменениями других ресурсов или удалением существующих объектов.

Из приватных outputs взять `registry_id` и `probe_secret_id`. Дополнить
`scope.json` полем `allowed_registry_id`. Собрать и проверить `infra/probe`,
загрузить образ в этот registry и записать его digest в `probe_image`.
В Lockbox отдельно загрузить `xray_config` и `web_credentials`; записать точный
ID версии в `secret_version_id`. Содержимое секретов не включать в Terraform.
Выбрать принятый digest приложения из `main`, проверить его происхождение,
скопировать его в тот же registry с сохранением digest.

## 3. Полное развёртывание

Выполнить `infra/deploy.sh plan PRIVATE_DIRECTORY`, проверить приватный план,
затем `infra/deploy.sh apply PRIVATE_DIRECTORY`. Сценарий проверяет область доступа,
применяет Terraform, задаёт лимиты масштабирования, обновляет состояние и вызывает
ограниченный runtime smoke. Это единственный поддерживаемый полный путь apply:
прямой Terraform apply не задаёт отсутствующие в провайдере лимиты масштабирования.

Если создание dashboard завершилось ошибкой `CreateDashboardMetadata`, воспользоваться
`recover-dashboard` по инструкции M1, затем повторить полный plan/apply.

Добавить DNS challenge из приватных outputs. После выпуска сертификата задать
`attach_domain=true` и повторить полный plan/apply. Проверить HTTPS, авторизацию
и отсутствие прямого анонимного доступа к объекту и контейнеру.

Расписание включается после ручного smoke и подготовки мониторинга. Для короткой
проверки допустим минутный `timer_schedule`; штатное расписание — раз в час.
Выключение: `enable_timer=false`, полный plan/apply и проверка отсутствия новых вызовов.

## 4. GitHub Environment

В `cloud-dev` разрешить только ветки инфраструктуры, перечисленные в workflow.
Разместить Environment Secrets: `M1_SCOPE_JSON`, `M1_CONFIG_JSON`, `M1_BACKEND_JSON`,
`M1_STATE_ACCESS_KEY`, `M1_STATE_SECRET_KEY`, `M1_DEPLOY_SERVICE_ACCOUNT`.
В них передаются проверенные приватные файлы и ключи, а не их пути.
Долгоживущий IAM-токен в GitHub не нужен: workflow получает его через OIDC.

Push проверяет план. Ручной запуск с `apply=true` применяет его и выполняет smoke.
Ручной dispatch требует наличия workflow в default branch GitHub; его публикация
туда проходит отдельную приёмку, без обхода запрета на merge. До этого ручное
развёртывание выполняется локальным сценарием, а проверка OIDC — push workflow.
Полные доказательства остаются в зашифрованном bucket; публичная сводка их не раскрывает.
