FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 tzdata ca-certificates
COPY collect.py /app/collect.py
COPY weight_min.py /app/weight_min.py
COPY mount_guard.py /app/mount_guard.py
COPY summary.py /app/summary.py
COPY run.sh /app/run.sh
RUN chmod 0755 /app/run.sh
LABEL io.hass.version="0.3.1" io.hass.type="app" io.hass.arch="aarch64"
CMD ["/app/run.sh"]