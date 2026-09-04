# Manual test deployment

This deployment is intentionally isolated under the Compose project
`zont-analyzer`. It publishes the narrow feedback API only on host loopback and
declares no external Docker network. The only shared host path is the exact static
`/za` directory selected through `ZONT_ANALYZER_PUBLISH_DIR` for
`compose.test.yaml`; the application itself contains no host-specific publishing
policy.

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

Edit `/opt/zont-analyzer/config.yaml` for the home. Generate an independent
feedback bearer key, then put the ZONT JSON containing
`token` and `email` in `/opt/zont-analyzer/secrets/zontaccesstoken.json`, and put
only the OpenAI key in
`/opt/zont-analyzer/secrets/openai_access_token.txt`:

```sh
umask 077
openssl rand -hex 32 > /opt/zont-analyzer/secrets/feedback_token.txt
```

These files are mounted
read-only and are not expanded into the Compose model or container environment.
Because the container is UID 10001, make each credential file owned by that UID
and private. Keep `.env` (release settings, not credentials) owned by root and mode
`0600`:

```sh
chown 10001:10001 /opt/zont-analyzer/secrets/zontaccesstoken.json \
  /opt/zont-analyzer/secrets/openai_access_token.txt \
  /opt/zont-analyzer/secrets/feedback_token.txt
chmod 0600 /opt/zont-analyzer/secrets/zontaccesstoken.json \
  /opt/zont-analyzer/secrets/openai_access_token.txt \
  /opt/zont-analyzer/secrets/feedback_token.txt
chown root:root /opt/zont-analyzer/.env
chmod 0600 /opt/zont-analyzer/.env
stat -c '%a %u:%g %n' /opt/zont-analyzer/.env /opt/zont-analyzer/secrets/*
```

Never put secrets in either Compose file or the release directory.
The production `.env` must keep `ZONT_ANALYZER_PUBLISH_DIR=/var/www/html/za`;
do not replace it with the example file during an upgrade.

Enable the daily archive index and add a same-origin nginx route beside the
existing static `/za/` location. Keep the archive rule as an exact match so
autoindex is not enabled for the rest of `/za/`. The container port remains
unreachable from external interfaces; the application still validates the
bearer key supplied by the HTML:

```nginx
location = /za/daily/ {
    autoindex on;
    autoindex_exact_size off;
    autoindex_localtime on;
}

location /za/api/ {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Validate nginx configuration before reloading it. Do not expose port 8787 on a
public address and do not place the bearer key in nginx configuration or HTML.

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
git tag release-1.7 YOUR_REVIEWED_COMMIT
git push origin release-1.7
```

CI publishes `ghcr.io/tupoybot/zontanalyzer:sha-COMMIT` with `GITHUB_TOKEN` and emits
`image.env` plus the exact `ghcr.io/...@sha256:...` reference in its summary. An existing
commit tag is reused. Deploy by digest, never by a moving tag. Publishing does not
connect to or deploy on the server. For private GHCR packages, authenticate Docker
on the server with an account allowed to read the package (`read:packages`);
use `docker login ghcr.io --password-stdin`, never a token in a command argument.
The package can remain private; no new application secrets are needed.

## Release preflight and deployment

Keep a Git checkout on the server for the small Compose files and deployment script;
check out the same reviewed commit as the image. Application source is not built
on the server. Existing `/opt/zont-analyzer/config.yaml`, `.env`, data, secrets and
`/var/www/html/za` stay in place. Preserve the previous checkout/reference for rollback.
Set `ZONT_ANALYZER_IMAGE` in `.env` to the digest emitted by CI. The old
`ZONT_ANALYZER_IMAGE_TAG` setting is no longer used.

Before replacing the running release, run the pulled candidate against a separately
writable online backup and a temporary non-public output directory. Inspect `initial`
and `daily` HTML/JSON. This can happen locally or in a separate container on the
production host; never mount live data/publication into the candidate. A clean-DB
bootstrap check uses another empty directory and read-only ZONT credentials.

The explicit deployment command pulls the digest, creates and verifies an online
backup using the running worker, preserves `.env.previous`, updates only the image
reference, starts Compose without a build, and checks health/status and lifecycle counts:

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
curl -fsS https://hk.tupoybot.ru/za/ >/dev/null
curl -fsS https://hk.tupoybot.ru/za/daily/ >/dev/null
curl -fsS https://hk.tupoybot.ru/za/latest.html >/dev/null
curl -fsS http://127.0.0.1:8787/api/health
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  -X PUT http://127.0.0.1:8787/api/recommendations/unknown/feedback)" = 401
docker ps --format '{{.Names}} {{.Status}}'
```

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
