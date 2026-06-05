FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/openace \
    ACESTREAM_HOST=127.0.0.1 \
    ACESTREAM_PORT=6878

WORKDIR /openace

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget curl ca-certificates cron logrotate \
        net-tools iftop bmon ffmpeg iproute2 && \
    rm -rf /var/lib/apt/lists/*

# Download and extract AceStream Engine
RUN wget -q "https://download.acestream.media/linux/acestream_3.2.11_ubuntu_22.04_x86_64_py3.10.tar.gz" && \
    tar zxf acestream_3.2.11_ubuntu_22.04_x86_64_py3.10.tar.gz && \
    rm acestream_3.2.11_ubuntu_22.04_x86_64_py3.10.tar.gz

# Install Python dependencies (separate layer for caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ /openace/app/
COPY server.py start.sh ./
COPY logrotate/acestream.conf /etc/logrotate.d/acestream

# Log files, data dir, and permissions
RUN chmod 644 /etc/logrotate.d/acestream && \
    mkdir -p /var/log/openace /openace/checkdb && \
    touch /var/log/openace/acestream.log /var/log/openace/proxy.log && \
    chmod 644 /var/log/openace/*.log && \
    chmod +x /openace/start.sh

# Daily logrotate cron job
RUN echo "0 0 * * * /usr/sbin/logrotate /etc/logrotate.conf" > /etc/cron.d/openace && \
    chmod 0644 /etc/cron.d/openace && \
    crontab /etc/cron.d/openace

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8888/ || exit 1

ENTRYPOINT ["/openace/start.sh"]
