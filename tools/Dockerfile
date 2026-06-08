FROM python:3.12.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl jq openssl ca-certificates mariadb-client mosquitto-clients \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pymysql influxdb-client paho-mqtt requests
WORKDIR /app
# scripts, certs and db/ are mounted at runtime; this container just idles for `docker exec`.
ENTRYPOINT ["sleep", "infinity"]
