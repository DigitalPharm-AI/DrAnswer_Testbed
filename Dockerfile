FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . ./

RUN mkdir -p /app/data

EXPOSE 8000 8001 8002

CMD ["uvicorn", "system_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
