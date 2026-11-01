# Product image: React frontend (built) + FastAPI backend. No OpenAI, no Pipecat, no engine code.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
# the only three engine-side modules the product reuses (pure Python: no pipecat, no openai)
COPY bot/role_config.py bot/guardrails.py bot/quote_validation.py bot/
COPY roles/5905.yaml roles/
COPY --from=web /web/dist web/dist
ENV DATA_DIR=/tmp/app-data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
