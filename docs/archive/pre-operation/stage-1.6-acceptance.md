# Приёмка этапа 1.6

Ветка: `stage-7.1`. Этап выполнен после уже закрытого 1.7 без переноса или слияния в другую ветку.

## Аудит пропущенных работ

Перед реализацией сверены фактический код, `implementation_plan.md`, `roadmap.md`, `status.md`,
история Git и приёмка 1.7. Этапы 1 и 1.5 присутствуют в приложении и тестах; этап 1.7 имеет
отдельную production-приёмку. Единственным пропущенным закрываемым этапом оказался 1.6.
Этап 1.8 явно описан как следующая будущая работа и не выдаётся за уже выполненный.

Необязательная часть 1.6 стала обязательной по факту production: прежняя логика считала сотни
обычных разрывов связи отказами котельного сервиса, хотя внутри большинства интервалов были
исторические точки котлового адаптера. Это существенно искажало MTBF/MTTR.

## Реализация

- `ReliabilityEvidenceSeries` хранит тип свидетельства, роль, origin, provenance, качество и
  timestamp. В событии сохраняются ID исходных ZONT-событий и использованной точки телеметрии.
- Каждый разрыв получает ровно один результат: `confirmed_service_running`,
  `confirmed_service_failure` или `unknown_service_impact`. Прямая валидная активность адаптера
  строго внутри завершённого интервала подтверждает продолжение сервиса; тепловая инерция,
  контекст ГВС и точки только до/после не используются как самостоятельное доказательство.
- Совпавшее отключение основного питания всегда остаётся подтверждённым `power_outage`.
  Только подтверждённые отказы входят в MTBF/MTTR; неизвестные интервалы сохраняются в context,
  а ненаблюдаемое восстановление не превращается в вычисленный MTTR.
- Внешний nginx защищает единым Basic Auth весь `/za/`: landing page, `latest.html`, архив и API.
  Feedback API по-прежнему слушает только host loopback. Из приложения, HTML, конфигурации и
  Compose удалены bearer-проверка и feedback-токен.
- CI содержит отдельный nginx E2E: все четыре маршрута без credentials возвращают 401, а с
  SHA-512 htpasswd работают HTML, архив, health и PUT feedback.

## Локальные проверки и production-копия

Штатный online backup создан на HK и затем проверен и использован только как отдельная локальная
записываемая копия:
`/opt/zont-analyzer/data/backups/zont-analyzer-20260904T214517743832Z.sqlite3` →
`/tmp/zont-stage16-production.sqlite3`. `integrity_check=ok`, ревизия `9f4a2c8d1e70`.
Кандидат не монтировал живую БД или `/var/www/html/za`.

Артефакты initial/daily без AI находятся в
`/tmp/zont-stage16-acceptance-20260905/{initial,daily}.{html,json,txt}`. Initial по всей доступной
истории сформирован примерно за 97 секунд локально. В HTML отсутствуют `sessionStorage`,
`Authorization`, `Bearer` и `feedback_token`.

Итог reconciliation на реальной истории:

| Результат | Интервалы |
| --- | ---: |
| `confirmed_service_running` | 651 |
| `unknown_service_impact` | 198 |
| `confirmed_service_failure` | 2 |

Оба подтверждённых отказа связаны с основным питанием. Initial MTBF — 628,442 часа; daily —
627,592 часа. Оба восстановления пересекают разрывы наблюдаемости, поэтому MTTR честно отсутствует.
Пример `confirmed_service_running` содержит ZONT loss/restore IDs и точку ряда 8 с ролью
`boiler_adapter_state`, origin `z3k_boiler_adapter` и её timestamp внутри интервала.

Локально прошли 127 тестов, Ruff, mypy, sdist/wheel build, runtime/test Docker builds,
feedback HTTP E2E, nginx Basic Auth E2E и валидация production/local Compose. Опубликованный
digest дополнительно запущен локально на той же изолированной production-копии; daily завершился
успешно.

## CI, образ и рабочее развёртывание

Коммит приложения: `9f09502cdac82d3127a48aebcf004138dceb6096`.
Релизный тег: `release-1.6-20260905`.
[CI/publish релиза](https://github.com/tupoybot/ZontAnalyzer/actions/runs/33924228481) прошёл успешно.

Рабочий образ:
`ghcr.io/tupoybot/zontanalyzer@sha256:d05164cec10c4f8806e888e6592f6154c0bbb8a1b9e896af2f66930d641ac83b`.
OCI revision совпадает с коммитом. Образ не собирался и не передавался на сервер вручную.

Из-за CPU-throttling VPS повторные тяжёлые проверки на HK не запускались. Развёртывание
переиспользовало уже созданный и локально проверенный online backup, скачало один digest и
пересоздало только `zont-analyzer-worker-1`. Полный initial, package checks и проверка кандидата
выполнены локально. Синтетические данные в рабочую БД не записывались.

`current` указывает на `/opt/zont-analyzer/releases/20260905-stage16-9f09502`. Первый старт
обнаружил оставшееся в постоянном `config.yaml` поле `feedback.token_file`; строгая схема
предсказуемо отказала. Поле удалено с резервной копией конфига, после чего новый рабочий цикл
завершился в `2026-09-04T22:18:20Z`: state `ok`, container healthy, `OOMKilled=false`.

nginx-конфигурация прошла `nginx -t`; htpasswd имеет `0640 root:www-data`. До удаления прежнего
plaintext-файла аутентифицированно проверены `/za/`, `/za/latest.html`, `/za/daily/`,
`/za/api/health` и GET существующего feedback — HTTP 200. Те же четыре публичных маршрута без
Basic Auth возвращают 401. Пароль пользователя `zont` — прежний feedback-ключ; он не выводился
в команды, логи или документы. После проверки `feedback_token.txt` удалён, в mount контейнера
его нет. nginx, Xray, Postfix, OpenDKIM и Docker остались active.

Свежий production daily JSON содержит те же 651/198/2 результата, MTBF 627,592 часа и не содержит
MTTR. `latest.html` обновлён штатным worker и не содержит bearer-кода.

**Этап 1.6 закрыт 2026-09-05 (Europe/Samara).** Следующая ещё не выполненная техническая работа —
этап 1.8; продуктовая оценка полезности P2 остаётся отдельным условием закрытия P2.
