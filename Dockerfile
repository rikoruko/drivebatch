FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Dynamically bind to the PORT environment variable provided by the host/platform
CMD exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 3600
