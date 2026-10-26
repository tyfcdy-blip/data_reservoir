FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home appuser
COPY data_service ./data_service
USER appuser
EXPOSE 8000
CMD ["uvicorn", "data_service.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
