# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY control_station_lite/ ./control_station_lite/

RUN pip install --no-cache-dir ".[server]"

EXPOSE 8080

CMD ["uvicorn", "control_station_lite.server.main:app", \
     "--host", "0.0.0.0", "--port", "8080"]
