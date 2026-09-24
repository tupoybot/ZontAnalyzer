FROM ghcr.io/xtls/xray-core@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7 AS xray
FROM python:3.12-slim AS package

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 zont \
    && useradd --system --uid 10001 --gid zont --home-dir /app zont
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Native YDB CLI for local checks and isolated data acceptance.
FROM package AS cli
LABEL org.zont.runtime="ydb-cli"
RUN mkdir -p /data /app/.access /config /publish \
    && chown -R 10001:10001 /data /app/.access /publish
USER zont
VOLUME ["/data"]
ENTRYPOINT ["zont-analyzer", "--data-dir", "/data"]
CMD ["run"]

FROM cli AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir '.[dev]' 'hatchling>=1.25'
COPY tests ./tests
USER zont
ENTRYPOINT ["pytest"]
CMD ["-q", "-p", "no:cacheprovider", "tests/integration/test_feedback_http.py"]

FROM package AS cloud
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
ARG REVISION=unknown
ENV CLOUD_REVISION=$REVISION
LABEL org.zont.runtime="cloud"
USER zont
ENTRYPOINT ["python", "-m", "zont_analyzer.cloud.runtime"]
CMD []

FROM cloud AS production
