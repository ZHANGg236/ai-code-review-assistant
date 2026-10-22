FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY storage.py ./
COPY folder_review.py ./
COPY review_bundles.py ./
COPY services ./services
COPY static ./static

EXPOSE 8000

# 本地 / VPS 默认 8000；Render、Railway 等平台会注入 PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips "*"
