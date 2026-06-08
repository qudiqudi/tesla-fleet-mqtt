FROM python:3.12.13-slim
RUN pip install --no-cache-dir paho-mqtt pymysql
COPY tlwriter.py /app/tlwriter.py
WORKDIR /app
ENTRYPOINT ["python", "/app/tlwriter.py"]
