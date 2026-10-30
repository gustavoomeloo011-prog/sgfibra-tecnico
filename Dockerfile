FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app_fastapi.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_WORKERS:-2} --proxy-headers"]
