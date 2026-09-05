# Приёмка этапа 1.8

Ветка: `stage-7.1`. Реализация календарного архива и отдельного домена без нового backend.

## Реализация и локальные проверки

- Единый publisher готовых завершённых daily/weekly/monthly; version-1 `reports.json`,
  атомарная запись отдельных файлов, архивы → manifest → latest, сериализация feedback.
- Vanilla JS календарь с пропусками дат, отдельными стрелками месяца, дневными кнопками,
  прямыми URL, точными недельными/месячными границами и same-origin feedback.
- Отдельный nginx root snippet/vhost `za.tupoybot.ru`; старый HK include сохраняется.
- Все 144 Python-теста прошли. Ruff/mypy, runtime/test Docker builds, installed-image
  publication, feedback HTTP E2E, nginx Basic Auth E2E и Compose validation прошли.
- Playwright Docker E2E проверяет 401 без Basic Auth для страницы/manifest/архива/API,
  навигацию по пропускам, недели/месяцы, feedback с обновлением страницы, legacy `/za/`,
  no-JS fallback и мобильную ширину 390 px без горизонтальной прокрутки.

Online backup HK:
`/opt/zont-analyzer/data/backups/zont-analyzer-20260905T060714417793Z.sqlite3`.
Локальная копия: `/tmp/zont-stage18/data/zont-analyzer.sqlite3`, отдельная публикация
`/tmp/zont-stage18/publish`. Исходный backup: `integrity_check=ok`, ревизия `9f4a2c8d1e70`.
Кандидат не монтировал живые данные или публикацию HK. Перепубликованы 123 завершённых
дневных периода, 2026-05-05–2026-09-04; 108 страниц содержат `ignored`. Просмотрены root,
2026-08-01 (3 ignored) и 2026-09-03 в Chromium без JS ошибок. Screenshots:
`/tmp/zont-stage18/{desktop,mobile}.png`.

В production-копии нет weekly/monthly: их списки честно пусты. Полные контракты этих
архивов проверены на реально рассчитанных локальных fixtures. Новых OpenAI-запросов
для приёмки не делалось.

Сертификат `za.tupoybot.ru` выпущен webroot Certbot 2026-09-05, действителен до 2026-12-04.
DNS имени указывает на HK `82.22.6.84`. Фактический nginx TLS listener — loopback 4443,
внешний 443 принадлежит Xray. На HK Telemt отсутствует; nginx и Xray active.

## CI и production

Коммит приложения: `24b1ba9e937357853e2ec1ace2bc35091375c594`.
Тег: `release-1.8-20260905`.
[Release CI](https://github.com/tupoybot/ZontAnalyzer/actions/runs/33949660599) и
[branch CI](https://github.com/tupoybot/ZontAnalyzer/actions/runs/33949660392) завершились успешно.
Пакет собран в sdist/wheel; полный новый nginx vhost проверен локально через `nginx -t`
и TLS Host/SNI на отдельном listener с тестовым сертификатом.

Образ:
`ghcr.io/tupoybot/zontanalyzer@sha256:858ae2d335c37a91c5544b61b8ddfd2432dc474611920618f06e2cfa3ed25af9`.
Локально скачан этот digest, SHA-256 всех 31 Python-файла совпали с коммитом; повторная
публикация на изолированной копии прошла. Финальный `integrity_check=ok`, foreign-key
нарушений нет; явные feedback, interventions и количество LLM-вызовов не изменились.

HK получил готовый образ без сборки и полных тестов. Release создал предрелизный online backup
`/opt/zont-analyzer/data/backups/zont-analyzer-20260905T062819219746Z.sqlite3`, обновил
только Compose worker и сохранил `.env.previous`. `current` указывает на
`/opt/zont-analyzer/releases/20260905-stage18-24b1ba9`.
Контейнер healthy, `OOMKilled=false`, OCI revision совпадает с коммитом. Первый рабочий цикл
завершён `2026-09-05T06:31:03Z`, state `ok`, manifest содержит 123 отчёта.

Установлены только новые `/etc/nginx/conf.d/30-zont-analyzer.conf` и
`/etc/nginx/snippets/zont-analyzer-root.conf`. После успешного `nginx -t` выполнен reload.
Для `za.tupoybot.ru` и legacy `hk.tupoybot.ru/za` проверены root, `latest.html`,
`reports.json`, `daily/`, `daily/2026-08-01.html`, `api/health`: без credentials 401,
с Basic Auth 200. GET feedback на обоих адресах вернул сохранённый `ignored`.
Сохранение feedback через PUT и обновление страницы проверены локальным Docker E2E;
на production для smoke данные владельца не переписывались.

Временный случайный пользователь smoke удалён сразу после проверки, исходный htpasswd
восстановлен побайтно; пароль владельца не читался и не менялся. Старый landing обновлён
ссылкой на основной домен. Legacy `/za/` и все его маршруты сохранены.

Хеши shared HK HTTP/TLS конфигов и всех Xray JSON совпали до/после cutover; 9 явных решений
владельца и 9 interventions сохранились побайтно на уровне выбранных SQL-строк.
`https://hk.tupoybot.ru/` и `https://kharichev.pro/` сохранили HTTP 200. nginx, Xray,
Postfix, OpenDKIM и Docker active; API опубликован только на `127.0.0.1:8787`.
Существующий Certbot deploy-hook обновления nginx сохранён; новый vhost содержит ACME webroot.

Откат домена: удалить только новый vhost, выполнить `nginx -t` и reload. Старый HK server
и Xray не менять. Откат приложения использует прежний digest из `.env.previous`.

**Этап 1.8 закрыт 2026-09-05 (Europe/Samara).** Продуктовая оценка полезности P2 владельцем
остаётся отдельным условием закрытия P2.
