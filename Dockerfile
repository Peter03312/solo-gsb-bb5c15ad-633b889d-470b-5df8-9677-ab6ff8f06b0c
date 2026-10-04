FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    ROYALTY_DB_PATH=/data/royalty.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（挂载卷即可持久化），启动时自动初始化数据库
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
