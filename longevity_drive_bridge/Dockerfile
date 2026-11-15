FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 ca-certificates
COPY replicate.py /app/replicate.py
COPY mount_guard.py /app/mount_guard.py
COPY run.sh /app/run.sh
RUN chmod 0755 /app/run.sh
LABEL io.hass.version="0.4.0" io.hass.type="app" io.hass.arch="aarch64"
CMD ["/app/run.sh"]