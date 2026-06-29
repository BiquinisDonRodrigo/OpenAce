FROM python:3.10-slim AS builder

ARG ACESTREAM_URL=https://download.acestream.media/linux/acestream_3.2.11_ubuntu_22.04_x86_64_py3.10.tar.gz
ARG ACESTREAM_SHA256=9b6bbd76a55e5a434641afae3b9cf8e6154ce1cf392152ec3aed5ac265432b2e

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q "$ACESTREAM_URL" -O acestream.tgz && \
    echo "$ACESTREAM_SHA256  acestream.tgz" | sha256sum -c - && \
    tar zxf acestream.tgz && \
    rm acestream.tgz

FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/openace \
    ACESTREAM_HOST=127.0.0.1 \
    ACESTREAM_PORT=6878

WORKDIR /openace

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates ffmpeg gosu iproute2 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/ /openace/

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /openace/app/
COPY server.py start.sh babel.cfg ./

# Compile i18n message catalogs (.po -> .mo)
RUN python -m babel.messages.frontend compile -d app/translations -D messages

RUN groupadd -r openace && useradd -r -g openace -d /openace -s /sbin/nologin openace && \
    mkdir -p /openace/checkdb /tmp/openace && \
    chown -R openace:openace /openace /tmp/openace && \
    chmod +x /openace/start.sh

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8888/healthz || exit 1

ENTRYPOINT ["/openace/start.sh"]
