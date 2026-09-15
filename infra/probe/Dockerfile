ARG XRAY_IMAGE=ghcr.io/xtls/xray-core@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7
FROM ${XRAY_IMAGE} AS xray
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY probe.py /app/probe.py
USER 65532:65532
ENTRYPOINT ["python", "/app/probe.py"]
