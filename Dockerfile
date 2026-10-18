FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY public/ ./public/
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
