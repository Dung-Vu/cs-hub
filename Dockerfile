FROM python:3.13-slim

WORKDIR /app

# WeasyPrint (PDF export + biên bản bàn giao) needs Pango/HarfBuzz at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/
COPY templates/ templates/
COPY config/ config/

RUN useradd --create-home --shell /bin/bash appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

EXPOSE 5100

ENV PYTHONUNBUFFERED=1

CMD ["gunicorn", "--bind", "0.0.0.0:5100", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "--pythonpath", "scripts", "app:app"]
