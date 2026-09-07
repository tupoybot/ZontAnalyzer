# Manual test deployment

This deployment is intentionally isolated under the Compose project
`zont-analyzer`. It publishes the narrow feedback API only on host loopback and
declares no external Docker network. nginx protects the complete `/za/` perimeter
with one Basic Auth policy; the application does not carry a second bearer secret.
The only shared host path is the exact static `/za` directory selected through
`ZONT_ANALYZER_PUBLISH_DIR` for `compose.test.yaml`; the application itself
contains no host-specific publishing policy.

## One-time host preparation

First audit, without changing anything:

```sh
ssh hk.tupoybot.ru 'hostname; date -Is; docker version 2>/dev/null || true; docker compose version 2>/dev/null || true; systemctl is-active nginx 2>/dev/null || true; ss -ltnp; df -h /opt; test -d /var/www/html && echo webroot-ok'
```

If Docker is absent, install the distribution's Docker Engine and Compose plugin.
Do not replace the existing nginx, firewall, Docker networks, or containers.
Before continuing, repeat the audit and check that the pre-existing workloads are
still healthy.

Create only the application's state and exact publication directories. The image
runs as UID/GID 10001; nginx only needs read access to the published file.

```sh
install -d -m 0750 -o 10001 -g 10001 /opt/zont-analyzer/data
install -d -m 0700 -o 10001 -g 10001 /opt/zont-analyzer/secrets
install -d -m 0755 -o 10001 -g 10001 /var/www/html/za
install -m 0644 RELEASE/deploy/config.production.example.yaml /opt/zont-analyzer/config.yaml
install -m 0600 RELEASE/deploy/env.example /opt/zont-analyzer/.env
test -e /var/www/html/za/index.html || \
  install -m 0644 RELEASE/deploy/site-index.html \
    /var/www/html/za/index.html
```

Edit `/opt/zont-analyzer/config.yaml` for the home. Put the ZONT JSON containing
`token` and `email` in `/opt/zont-analyzer/secrets/zontaccesstoken.json`, and put
only the OpenAI key in `/opt/zont-analyzer/secrets/openai_access_token.txt`.
These files are mounted read-only and are not expanded into the Compose model or
container environment.
Because the container is UID 10001, make each credential file owned by that UID
and private. Keep `.env` (release settings, not credentials) owned by root and mode
`0600`:

```sh
chown 10001:10001 /opt/zont-analyzer/secrets/zontaccesstoken.json \
  /opt/zont-analyzer/secrets/openai_access_token.txt
chmod 0600 /opt/zont-analyzer/secrets/zontaccesstoken.json \
  /opt/zont-analyzer/secrets/openai_access_token.txt
chown root:root /opt/zont-analyzer/.env
chmod 0600 /opt/zont-analyzer/.env
stat -c '%a %u:%g %n' /opt/zont-analyzer/.env /opt/zont-analyzer/secrets/*
```

Never put secrets in either Compose file or the release directory.
The production `.env` must keep `ZONT_ANALYZER_PUBLISH_DIR=/var/www/html/za`;
do not replace it with the example file during an upgrade.

Create a private htpasswd file outside the release and application directories.
Use an interactive password prompt so the password does not enter Git, shell
history, HTML, logs, or application configuration:

```sh
test -e /etc/nginx/zont-analyzer.htpasswd || \
  install -m 0640 -o root -g www-data /dev/null /etc/nginx/zont-analyzer.htpasswd
htpasswd /etc/nginx/zont-analyzer.htpasswd zont
```

Install `deploy/nginx-zont-analyzer.conf` as an nginx snippet and include it once
inside the TLS `server` block for `hk.tupoybot.ru`. Remove older `/za/daily/` and
`/za/api/` locations from that block; the snippet owns all `/za/` routes:

```sh
install -m 0644 RELEASE/deploy/nginx-zont-analyzer.conf \
  /etc/nginx/snippets/zont-analyzer.conf
```

```nginx
include /etc/nginx/snippets/zont-analyzer.conf;
```

Validate the complete nginx configuration before reloading it. Without Basic
Auth, `/za/`, `latest.html`, `/za/daily/`, and `/za/api/` must all return 401;
with the same credentials, reports and feedback must work. Keep port 8787 on host
loopback and do not put the Basic Auth password in application files.

## Local development and CI

Develop and debug locally with the normal `.venv` commands from README. The local
Compose file uses its own named volume and has no host production mounts:

```sh
docker compose -f deploy/compose.local.yaml build
docker compose -f deploy/compose.local.yaml run --rm worker init
docker compose -f deploy/compose.local.yaml run --rm worker analyze daily --no-ai
docker compose -f deploy/compose.local.yaml run --rm --entrypoint sh worker
```

For a real-data check, mount a verified online SQLite **copy** to an isolated
container. Do not attach the candidate to live data. Local credentials can be
mounted read-only explicitly when needed; they never go into the image or CI.

Every branch/PR runs tests, Ruff, mypy, wheel installation, Docker build and feedback
HTTP E2E. To publish a reviewed commit, push an explicit `release-*` tag, or run CI
manually with `publish=true`. The release still has to pass the test job:

```sh
git tag release-1.6-YYYYMMDD YOUR_REVIEWED_COMMIT
git push origin release-1.6-YYYYMMDD
```

CI publishes `ghcr.io/tupoybot/zontanalyzer:sha-COMMIT` with `GITHUB_TOKEN` and emits
`image.env` plus the exact `ghcr.io/...@sha256:...` reference in its summary. An existing
commit tag is reused. Deploy by digest, never by a moving tag. Publishing does not
connect to or deploy on the server. For private GHCR packages, authenticate Docker
on the server with an account allowed to read the package (`read:packages`);
use `docker login ghcr.io --password-stdin`, never a token in a command argument.
The package can remain private; no new application secrets are needed.

## HK load policy

Never build or run full tests on HK: it shares capacity with other workloads and
the hosting provider has complained about sustained load. Build and test locally
or in CI, including analysis and full SQLite integrity checks on a downloaded
online backup copy. HK receives only the tested registry digest. After deployment,
limit verification to container health/worker status, a few HTTP requests and
small read-only metadata queries. Do not run full initial analysis, bootstrap,
backfill, benchmarks or repeated `doctor` checks on HK for acceptance.

## Stage 1.8 dedicated report domain

`https://za.tupoybot.ru/` is the main entry point and serves `latest.html` directly.
The archive manifest `reports.json` lists only existing completed daily/weekly/monthly
reports; `start`/`end` are local dates, with the end boundary excluded. All public
artifacts are replaced atomically, archives before the manifest and latest last;
a failed write cannot expose partial JSON/HTML. Writers, including feedback, share
a filesystem lock. This is ordered per-file publication, not a multi-file transaction.

To refresh existing reports with no analysis or external calls:

```sh
zont-analyzer --config CONFIG --data-dir DATA report publish
```

Validate `deploy/nginx-zont-analyzer-root.conf` locally with the Docker browser E2E.
It owns the dedicated root, uses the existing Basic Auth file, and proxies `/api/`
to the loopback application's existing `/za/api/` prefix. The separate
`deploy/nginx-za-vhost.conf` listens on HK's existing `127.0.0.1:4443` TLS fallback;
Xray retains port 443. Never copy this root snippet into the shared HK vhost.

Before cutover, verify DNS, issue the dedicated certificate with Certbot webroot
(`/var/www/certbot`), validate the candidate TLS configuration locally, and record
hashes of existing HK nginx/Xray configs and responses of existing external endpoints.
Install only the two new files, run `nginx -t`, reload once, and check Basic Auth,
the root, manifest, direct archive and API URLs. Certbot renewal uses the separate
HTTP ACME location in the new vhost. Retain the existing renewal/reload hook.
The owner's Basic Auth credential is unchanged. A temporary random acceptance
user may be added for the bounded authenticated smoke and must be removed immediately.

Legacy `https://hk.tupoybot.ru/za/` and its include remain active. Rollback of the
domain removes only `/etc/nginx/conf.d/30-zont-analyzer.conf`, then runs `nginx -t`
and reloads. Do not edit shared HK or Xray configuration during cutover or rollback.

## Release preflight and deployment

Keep a Git checkout on the server for the small Compose files and deployment script;
check out the same reviewed commit as the image. Application source is not built
on the server. Existing `/opt/zont-analyzer/config.yaml`, `.env`, data, secrets and
`/var/www/html/za` stay in place. Preserve the previous checkout/reference for rollback.
Pass the digest emitted by CI to `release.sh`; it saves the previous `.env` before
updating `ZONT_ANALYZER_IMAGE`. For first installation, set that variable manually.
The old `ZONT_ANALYZER_IMAGE_TAG` setting is no longer used by the new Compose file.

Before replacing the running release, run the pulled candidate against a separately
writable online backup and a temporary non-public output directory. Inspect `initial`
and `daily` HTML/JSON locally; never run candidate acceptance on HK or mount live
data/publication into the candidate. A clean-DB
bootstrap check uses another empty directory and read-only ZONT credentials.

When migrating an existing bearer deployment, enable and verify nginx Basic Auth
before starting the bearer-free application image. This makes feedback briefly
unavailable instead of briefly writable without authentication. The old
`feedback_token.txt` may be removed only after the new image and the external
Basic Auth feedback path have both been verified.

The explicit deployment command pulls the digest, creates and verifies an online
backup using the running worker, preserves `.env.previous`, updates only the image
reference, starts Compose without a build, and waits for container health:

```sh
./deploy/release.sh ghcr.io/tupoybot/zontanalyzer@sha256:YOUR_VERIFIED_DIGEST
```

It does not deploy on every push. If a check fails, inspect the command output and
worker logs before retrying. Do not automatically restore SQLite. The first updated
startup also creates a verified pre-migration backup and logs the number of old `new`
recommendations before marking them `ignored`; later maintenance is idempotent.

For first installation only, after host preparation and selecting the digest:

Start only this Compose project; never use `docker compose down -v` or global
Docker prune commands on this shared server.

```sh
cd /opt/zont-analyzer/current
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml pull worker
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml up -d --no-build
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml ps
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml logs --tail 100 worker
```

The worker atomically updates `latest.html`, the `daily/` archive, and its
heartbeat. To create the one-off initial AI report after telemetry is available,
run analysis once and export the newly stored report to the stable AI link:

```sh
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml \
  exec -T worker zont-analyzer --config /config/config.yaml --data-dir /data \
  analyze initial --days 90
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml \
  exec -T worker zont-analyzer --config /config/config.yaml --data-dir /data \
  report export --format html --output /publish/ai-latest.html
```

Verify `worker` is healthy, the stable report files are non-empty, the site
returns the landing page, and unrelated workloads remain unchanged:

```sh
test -s /var/www/html/za/index.html
test -s /var/www/html/za/latest.html
test -s /var/www/html/za/ai-latest.html
curl -fsS http://127.0.0.1:8787/za/api/health
for path in /za/ /za/daily/ /za/latest.html /za/api/health; do
  test "$(curl -sS -o /dev/null -w '%{http_code}' \
    "https://hk.tupoybot.ru$path")" = 401
  curl -fsS -u zont "https://hk.tupoybot.ru$path" >/dev/null
done
docker ps --format '{{.Names}} {{.Status}}'
```

The authenticated public `curl` commands prompt for the Basic Auth password via
`curl -u zont`. The loopback health endpoint intentionally has no
application-level authentication; its host binding is the security boundary.

## Rollback

An application rollback is non-destructive: retain `/opt/zont-analyzer/data`, point
`current` back to the previous immutable release, select its original `ZONT_ANALYZER_IMAGE` digest in
the root-only `.env`, validate Compose, and run `up -d --no-build` again. This
replaces only containers in project `zont-analyzer` and leaves SQLite and the
published last-known-good page intact.

If the older application reports an incompatible database schema, stop the
rollback. Do not overwrite the live database. Keep the worker stopped, identify
the verified pre-migration/online backup under `/opt/zont-analyzer/data/backups`,
and perform a separately approved restore to a new filename first. Validate that
copy with SQLite integrity checks before any explicit cutover.

To disable only test publication while preserving data, set
`pilot.reports_dir: reports` in the config and redeploy the worker with
`deploy/compose.yaml` alone. Do not delete the `/za` directory as part of rollback.

Registry workflow references: [GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[Compose pull](https://docs.docker.com/reference/cli/docker/compose/pull/).

## Импорт локально пересчитанных отчётов и графиков

При переносе готовых canonical-отчётов переносите и `chart-data-cache`, построенный
тем же проверенным образом на локальной online-копии. Кэш привязан к digest отчёта:
одного переноса SQLite-контекста недостаточно, иначе первая публикация заново прочитает
историю для графиков. Такое построение на HK не использовать как приёмку.

До запуска нового worker выполните на остановленной рабочей БД ограниченный импорт:

```sh
python3 deploy/import_analysis.py /opt/zont-analyzer/data/zont-analyzer.sqlite3 \
  derived-payload.json --chart-cache prepared-chart-data-cache
```

Payload содержит только подготовленные отчёты с optimistic guards, разрешённые
газовые метаданные и новые записи учёта AI/рекомендаций. Импорт показаний, профиля и
feedback этим инструментом не допускается. Предварительно сверяйте владельческие
данные с принятой локальной копией; при конфликте повторяйте подготовку локально.
`--chart-cache` проверяет имена файлов, схему и точное совпадение canonical digest
всего пакета перед установкой. Владельцем кэша становится владелец БД, файлы имеют
режим 0600. Повторный импорт идемпотентен. Миграция БД не требуется.

При обычном вводе/исправлении газа приложение само сохраняет существующие графики,
если изменился только газовый контекст. Изменение тепловых фактов или периода
не разрешает такое переиспользование. Доказательства выпуска этапа 8:
[stage-8-acceptance.md](../docs/archive/pre-operation/stage-8-acceptance.md).
