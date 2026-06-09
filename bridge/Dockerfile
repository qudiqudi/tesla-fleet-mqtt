FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt requests
# /data holds the rotated refresh token; world-writable so a named volume mounted here
# is writable whatever PUID/PGID the container runs as (the token file itself is 0600).
RUN mkdir -p /data && chmod 777 /data
COPY bridge.py /app/bridge.py
WORKDIR /app
ENTRYPOINT ["python", "/app/bridge.py"]
