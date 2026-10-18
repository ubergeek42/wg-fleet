# WireGuard Fleet Manager - justfile

# Default recipe - show available commands
default:
    @just --list

# Install Python dependencies (including test dependencies)
install:
    pip install -r requirements-dev.txt

# Run the fleet server with default config
run: check-root
    python3 main.py

# Run the fleet server with custom config
run-config CONFIG: check-root
    python3 main.py --config {{CONFIG}}

# Run the server with the example config
run-example: check-root
    python3 main.py --config example-config.yaml

# Run all tests
test:
    pytest -v

# Run specific test file
test-file FILE:
    pytest -v {{FILE}}

# Run tests with coverage
test-coverage:
    pytest --cov=. --cov-report=html --cov-report=term

# Run integration tests only
test-integration:
    pytest -v test_integration.py

# Check code formatting with black (if installed)
format:
    black *.py hooks/

# Lint code with ruff (if installed)
lint:
    ruff check *.py hooks/

# Check if running as root (required for WireGuard management)
check-root:
    #!/usr/bin/env bash
    if [ "$EUID" -ne 0 ]; then
        echo "ERROR: This command must be run as root (required for WireGuard interface management)"
        exit 1
    fi

# Clean up Python cache files
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
    rm -rf .pytest_cache htmlcov .coverage

# Show WireGuard interface status for all fleets
wg-status: check-root
    @echo "=== WireGuard Interface Status ==="
    @for iface in /etc/wireguard/wg*.conf; do \
        if [ -f "$iface" ]; then \
            name=$(basename "$iface" .conf); \
            echo ""; \
            echo "Interface: $name"; \
            wg show "$name" 2>/dev/null || echo "  (not running)"; \
        fi \
    done

# Bring down all fleet interfaces
wg-down: check-root
    @echo "Bringing down all fleet interfaces..."
    @for iface in /etc/wireguard/wg*.conf; do \
        if [ -f "$iface" ]; then \
            name=$(basename "$iface" .conf); \
            echo "Stopping $name..."; \
            wg-quick down "$name" 2>/dev/null || true; \
        fi \
    done

# View the generated fleet hosts file
show-hosts:
    cat /run/wg_fleet_hosts 2>/dev/null || echo "No hosts file found at /run/wg_fleet_hosts"

# Tail server logs (if running as systemd service)
logs:
    journalctl -u wg-fleet -f

# View database contents
show-db:
    sqlite3 /var/lib/wg-fleet/clients.db "SELECT fleet_id, hostname, assigned_ip, timestamp FROM clients ORDER BY fleet_id, timestamp DESC;"

# Create example systemd service file
create-service:
    @echo "[Unit]" > wg-fleet.service
    @echo "Description=WireGuard Fleet Manager" >> wg-fleet.service
    @echo "After=network.target" >> wg-fleet.service
    @echo "" >> wg-fleet.service
    @echo "[Service]" >> wg-fleet.service
    @echo "Type=simple" >> wg-fleet.service
    @echo "ExecStart=$(pwd)/main.py --config /etc/wg-fleet.yaml" >> wg-fleet.service
    @echo "Restart=on-failure" >> wg-fleet.service
    @echo "RestartSec=10s" >> wg-fleet.service
    @echo "StandardOutput=journal" >> wg-fleet.service
    @echo "StandardError=journal" >> wg-fleet.service
    @echo "" >> wg-fleet.service
    @echo "[Install]" >> wg-fleet.service
    @echo "WantedBy=multi-user.target" >> wg-fleet.service
    @echo "Created wg-fleet.service - copy to /etc/systemd/system/ and enable with systemctl"

# Container management

# Build the container image
build-container:
    podman build -t wg-fleet:latest .

# Run container in privileged mode on host network with default config
run-container: build-container
    mkdir -p data
    podman run --rm \
        --privileged \
        --network host \
        --cap-add NET_ADMIN \
        --cap-add SYS_MODULE \
        -v /etc/wg-fleet.yaml:/etc/wg-fleet.yaml:ro \
        -v /etc/wireguard:/etc/wireguard \
        -v $(pwd)/data:/var/lib/wg-fleet \
        -v /run:/run \
        --name wg-fleet \
        wg-fleet:latest

# Run container with example config
run-container-example: build-container
    mkdir -p data
    podman run --rm \
        --privileged \
        --network host \
        --cap-add NET_ADMIN \
        --cap-add SYS_MODULE \
        -v $(pwd)/example-config.yaml:/etc/wg-fleet.yaml:ro \
        -v /etc/wireguard:/etc/wireguard \
        -v $(pwd)/data:/var/lib/wg-fleet \
        -v /run:/run \
        --name wg-fleet \
        wg-fleet:latest

# Run container with custom config
run-container-config CONFIG: build-container
    mkdir -p data
    podman run --rm \
        --privileged \
        --network host \
        --cap-add NET_ADMIN \
        --cap-add SYS_MODULE \
        -v {{CONFIG}}:/etc/wg-fleet.yaml:ro \
        -v /etc/wireguard:/etc/wireguard \
        -v $(pwd)/data:/var/lib/wg-fleet \
        -v /run:/run \
        --name wg-fleet \
        wg-fleet:latest

# Run container in detached mode
run-container-daemon: build-container
    mkdir -p data
    podman run -d \
        --privileged \
        --network host \
        --cap-add NET_ADMIN \
        --cap-add SYS_MODULE \
        -v /etc/wg-fleet.yaml:/etc/wg-fleet.yaml:ro \
        -v /etc/wireguard:/etc/wireguard \
        -v $(pwd)/data:/var/lib/wg-fleet \
        -v /run:/run \
        --name wg-fleet \
        --restart always \
        wg-fleet:latest
    @echo "Container started in background. Use 'just container-logs' to view logs."

# Stop the running container
stop-container:
    podman stop wg-fleet || true

# View container logs
container-logs:
    podman logs -f wg-fleet

# Remove container image
clean-container:
    podman rmi wg-fleet:latest || true
