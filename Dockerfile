FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --system zont && useradd --system --gid zont --home-dir /app zont
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
RUN mkdir -p /data && chown zont:zont /data
USER zont
VOLUME ["/data"]
ENTRYPOINT ["zont-analyzer", "--data-dir", "/data"]
CMD ["run"]
