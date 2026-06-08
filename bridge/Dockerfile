FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt requests
COPY bridge.py /app/bridge.py
WORKDIR /app
ENTRYPOINT ["python", "/app/bridge.py"]
