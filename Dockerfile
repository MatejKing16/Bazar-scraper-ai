# Use lightweight official Python 3.11 slim image
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=5050

# Install security updates and curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside container
WORKDIR /app

# Install Python dependencies first for caching layers
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app

# Ensure data directory exists
RUN mkdir -p /app/data

# Persistent data volume
VOLUME ["/app/data"]

# Expose web dashboard port
EXPOSE 5050

# Automated container healthcheck (legit check)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5050/api/status || exit 1

# Default execution command
CMD ["python", "main.py"]
