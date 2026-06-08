FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt pymysql
COPY sessionizer.py /app/sessionizer.py
WORKDIR /app
ENTRYPOINT ["python", "/app/sessionizer.py"]
