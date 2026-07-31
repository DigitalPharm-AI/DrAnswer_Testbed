FROM node:22-slim AS frontend-build

WORKDIR /app

COPY frontend/package*.json ./frontend/
RUN npm --prefix frontend ci

COPY frontend ./frontend
RUN npm --prefix frontend run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . ./
COPY --from=frontend-build /app/system_app/static/react ./system_app/static/react

RUN mkdir -p /app/data

EXPOSE 8000 8001 8701

CMD ["uvicorn", "system_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
