FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        openssh-client \
        rsync \
        tini \
        vnstat \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY source/forwarder /app/forwarder
COPY source/archive /app/archive
COPY source/watch115 /app/watch115
COPY tests /app/tests
COPY container/supervisord.conf /etc/supervisor/conf.d/tg-media-suite.conf
COPY container/healthcheck.py /app/healthcheck.py

RUN python -m unittest discover -s /app/tests -v

RUN mkdir -p /data/forwarder /data/archive /data/forwarder/watch115/staging \
        /var/log/tg-media-suite \
    && chmod 0755 /app/healthcheck.py

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/tg-media-suite.conf"]
