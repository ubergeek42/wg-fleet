FROM python:3.14-slim

# Install WireGuard tools and other dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wireguard-tools \
    iproute2 \
    iptables \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Create application directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY templates/ ./templates/

# Create necessary directories
RUN mkdir -p /etc/wireguard /run

# Expose web server port
EXPOSE 8000

# Run as root (required for WireGuard management)
USER root

# Set entrypoint
ENTRYPOINT ["python3", "main.py"]

# Default to looking for config at /etc/wg-fleet.yaml
CMD ["--config", "/etc/wg-fleet.yaml"]
