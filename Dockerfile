# MIOS 2.0 — Docker image
# Build: docker build -t mios .
# Run:   docker run --env-file .env mios

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cached separately from source)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY mios/ ./mios/

# ENTRYPOINT keeps the python invocation fixed; CMD provides the default job.
# Cloud Scheduler overrides CMD args (e.g. --run opening_scan) via
# containerOverrides.args in the message body.
# Running the container without args defaults to the pre-market report.
ENTRYPOINT ["python", "-m", "mios.main"]
CMD ["--run", "pre_market"]
