FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --system --gid 10001 zont \
    && useradd --system --uid 10001 --gid zont --home-dir /app zont
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
RUN mkdir -p /data /app/.access /config /publish \
    && chown -R 10001:10001 /data /app/.access /publish
USER zont
VOLUME ["/data"]
ENTRYPOINT ["zont-analyzer", "--data-dir", "/data"]
CMD ["run"]

FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir '.[dev]' 'hatchling>=1.25'
COPY tests ./tests
USER zont
ENTRYPOINT ["pytest"]
CMD ["-q", "-p", "no:cacheprovider", "tests/integration/test_feedback_http.py"]

FROM runtime AS production
