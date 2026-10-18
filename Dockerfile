# Deploy lên Render: docker build từ thư mục gốc repo (megabot)
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Gói hệ thống cho xử lý âm thanh Xiaozhi
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 libopus-dev build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY main/xiaozhi-server/requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt

COPY main/xiaozhi-server/ .
RUN mkdir -p /app/data /app/tmp

# Render cấp PORT động (mặc định 10000). WebSocket + /health lắng nghe 0.0.0.0:$PORT
ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
