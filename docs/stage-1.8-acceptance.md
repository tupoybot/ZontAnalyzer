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

Публикация immutable образа через CI и короткая production-проверка выполняются после
локальной приёмки. До подтверждения этих шагов этап не считается закрытым.
