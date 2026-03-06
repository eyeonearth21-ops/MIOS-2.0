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

# Default: start the scheduler daemon
CMD ["python", "-m", "mios.main"]
