FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt influxdb-client
COPY historian.py /app/historian.py
WORKDIR /app
ENTRYPOINT ["python", "/app/historian.py"]
