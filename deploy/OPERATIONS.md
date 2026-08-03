# Manual test deployment

This deployment is intentionally isolated under the Compose project
`zont-analyzer`. It publishes no ports and declares no external Docker network.
The only shared host path is the exact static `/za` directory selected through
`ZONT_ANALYZER_PUBLISH_DIR` for `compose.test.yaml`; the application itself
contains no host-specific publishing policy.

## One-time host preparation

First audit, without changing anything:

```sh
ssh 217.60.10.224 'hostname; date -Is; docker version 2>/dev/null || true; docker compose version 2>/dev/null || true; systemctl is-active nginx 2>/dev/null || true; ss -ltnp; df -h /opt; test -d /opt/nightscout-compose/certbot/www/tupoybot.ru/html && echo webroot-ok'
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
install -d -m 0755 -o 10001 -g 10001 /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za
install -m 0644 RELEASE/deploy/config.production.example.yaml /opt/zont-analyzer/config.yaml
install -m 0600 RELEASE/deploy/env.example /opt/zont-analyzer/.env
test -e /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za/index.html || \
  install -m 0644 RELEASE/deploy/site-index.html \
    /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za/index.html
```

Edit `/opt/zont-analyzer/config.yaml` for the home. Put the ZONT JSON containing
`token` and `email` in `/opt/zont-analyzer/secrets/zontaccesstoken.json`, and put
only the OpenAI key in
`/opt/zont-analyzer/secrets/openai_access_token.txt`. These files are mounted
read-only and are not expanded into the Compose model or container environment.
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

## Release layout and preflight

Upload each source snapshot to an immutable directory such as
`/opt/zont-analyzer/releases/20260803-021500`, then atomically point
`/opt/zont-analyzer/current` to it. Preserve previous releases for rollback.
Do not upload `.access`, `.git`, local SQLite files, `.env`, caches, or reports.

Set a unique `ZONT_ANALYZER_IMAGE_TAG` in `/opt/zont-analyzer/.env` for the release.
From the release directory, validate interpolation before building (the rendered
output contains variable names but must not be copied into logs if future Compose
changes inline a secret):

```sh
cd /opt/zont-analyzer/current
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml config --quiet
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml build --pull
```

Run one-off preflight commands with the built image. `doctor --live` performs one
read-only ZONT request; omit `--live` if the API must not be contacted yet.

```sh
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml run --rm --no-deps worker init
docker compose --project-name zont-analyzer \
  --env-file /opt/zont-analyzer/.env \
  -f deploy/compose.yaml -f deploy/compose.test.yaml run --rm --no-deps worker doctor --live
```

## Backup and deploy

Before replacing a running release, make an online verified SQLite backup with the
old release. Do not `cp` the live `.sqlite3`, `-wal`, and `-shm` files separately.

```sh
/opt/zont-analyzer/current/deploy/backup-sqlite.sh /opt/zont-analyzer/current
```

Start only this Compose project; never use `docker compose down -v` or global
Docker prune commands on this shared server.

```sh
cd /opt/zont-analyzer/current
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
test -s /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za/index.html
test -s /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za/latest.html
test -s /opt/nightscout-compose/certbot/www/tupoybot.ru/html/za/ai-latest.html
curl -fsS https://tupoybot.ru/za/ >/dev/null
docker ps --format '{{.Names}} {{.Status}}'
```

## Rollback

An application rollback is non-destructive: retain `/opt/zont-analyzer/data`, point
`current` back to the previous immutable release, select its original image tag in
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
