FROM python:3.11-slim

# Install system dependencies (including curl for health checks)
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY templates/ ./templates/

# Create non-root user for security
RUN groupadd -r altstore && useradd -r -g altstore altstore \
    && mkdir -p /app/data \
    && chown -R altstore:altstore /app

# Switch to non-root user
USER altstore

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/source.json || exit 1

# Run the app
CMD ["python", "app.py"]
# FROM python:3.11-slim
#
# WORKDIR /app
#
# # Install system dependencies including curl for healthcheck
# RUN apt-get update && apt-get install -y \
#     gcc \
#     curl \
#     && rm -rf /var/lib/apt/lists/* \
#     && apt-get clean
#
# # Copy requirements first for better caching
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
#
# # Copy application files
# COPY app.py .
#
# # Create data directory for source.json
# RUN mkdir -p /app/uploads
#
# # Expose port
# EXPOSE 5000
#
# # Set environment variables
# ENV FLASK_APP=app.py
# ENV PYTHONUNBUFFERED=1
#
# # Run the application
# CMD ["python", "-u", "app.py"]
