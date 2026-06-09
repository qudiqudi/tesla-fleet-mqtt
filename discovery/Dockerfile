FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt
COPY discovery.py /app/discovery.py
WORKDIR /app
ENTRYPOINT ["python", "/app/discovery.py"]
