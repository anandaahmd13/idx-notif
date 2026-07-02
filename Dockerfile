# Official Playwright image ships Chromium + all system deps pre-installed,
# which saves fighting with libnss/libatk/etc. Keep this tag in sync with the
# playwright version resolved by requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY idxbot/ ./idxbot/
COPY config.yaml ./config.yaml

# State (seen.json) is written to /app; mount a volume there to persist it
# across restarts (see docker-compose.yml).
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "idxbot"]
