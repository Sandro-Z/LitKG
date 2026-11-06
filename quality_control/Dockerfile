FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QC_BIND=0.0.0.0 QC_PORT=8090 QC_DB_PATH=/data/quality.sqlite3
WORKDIR /opt/litkg-qc
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 qc && useradd --uid 10001 --gid qc --no-create-home qc && mkdir /data && chown qc:qc /data
COPY --chown=qc:qc quality_control ./quality_control
USER qc
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)"
CMD ["python", "-m", "quality_control.server"]
