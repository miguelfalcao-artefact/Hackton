FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies in their own layer so code edits do not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The log sink lives here. Created and chowned before dropping privileges,
# otherwise the non-root user cannot open the file and the sink silently
# degrades to stdout only.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/log/app \
    && chown -R appuser:appuser /var/log/app /srv
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=4s --retries=24 --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
