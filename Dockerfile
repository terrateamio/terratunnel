# Multi-stage build for smaller final image
FROM python:3.11-alpine AS builder

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apk add --no-cache gcc musl-dev libffi-dev

# Set working directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --target=/install -r requirements.txt

# Copy application code
COPY terratunnel/ terratunnel/

# Production stage
FROM python:3.11-alpine

# Set metadata
LABEL maintainer="Terratunnel Team <hello@terrateam.io>" \
      description="A secure HTTP tunnel server and client for forwarding webhooks to local development environments" \
      version="1.0.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user for security
RUN addgroup -S terratunnel && adduser -S terratunnel -G terratunnel

# Install runtime dependencies only
RUN apk add --no-cache libffi

# Copy installed packages and application code from builder
COPY --from=builder /install /usr/local/lib/python3.11/site-packages
COPY --from=builder /app/terratunnel /app/terratunnel

# Set Python path to include the app directory
ENV PYTHONPATH=/app:$PYTHONPATH
WORKDIR /app

# Switch to non-root user
USER terratunnel

# Expose common ports
EXPOSE 8000 8080 8081

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -m terratunnel --help > /dev/null || exit 1

# Default command (can be overridden)
ENTRYPOINT ["python", "-m", "terratunnel"]
CMD ["--help"]
