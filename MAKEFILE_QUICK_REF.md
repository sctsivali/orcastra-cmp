# Makefile Quick Reference Card

## Most Used Commands

```bash
# Help & Info
make help                    # Show all available commands
make version                 # Show app version and build info
make status                  # Show service status
make health                  # Check health of all services

# Version Management (operator VM only: these come from its local.mk)
make version-check           # Check version consistency
make version-sync            # Sync version from .env to all files
make version-set VERSION=x.x.x  # Set new version and sync

# Start & Stop
make up                      # Start all services
make down                    # Stop all services
make restart                 # Restart all services
make restart-backend         # Restart backend only
make restart-frontend        # Restart frontend only

# Build
make build                   # Build all images
make build-nc                # Build without cache (clean build)
make build-backend-nc        # Rebuild backend without cache

# Logs
make logs                    # Follow all logs
make logs-backend            # Follow backend logs
make logs-frontend           # Follow frontend logs
make logs-backend-last       # Show last 100 lines (backend)

# Development
make dev                     # Run all with logs visible
make shell-backend           # Shell into backend container
make redis-cli               # Open Redis CLI

# Checks
make ci-local                # Every CI job on this machine, run before a push
make ci-local STAGES=backend # One stage: commits guards backend frontend e2e

# Quick Actions
make quick-start             # First time setup and start
make quick-restart           # Quick restart everything
make quick-rebuild           # Full rebuild and restart
```

## One-Liners for Common Tasks

```bash
# Update version untuk release (operator VM only)
make version-set VERSION=1.0.0 && make quick-rebuild

# Check if everything is running
make status && make health

# Restart backend and watch logs
make restart-backend && make logs-backend

# Full rebuild after code changes
make down && make build-nc && make up

# Or simply:
make quick-rebuild

# Check recent errors in backend
make logs-backend-last | grep ERROR

# Monitor health status
watch -n 5 make health

# Backup Redis before changes
make backup-redis

# Clean everything and start fresh
make down-v && make prune && make quick-start
```

## Vault PKI Commands

These targets exist only on the operator VM (its local.mk), like the `scripts/vault_pki_manager.sh` they call.

```bash
# Check PKI certificate status (Root CA & Intermediate CA expiry)
make vault-status

# Renew Intermediate CA certificate (interactive)
make vault-renew

# Show Vault PKI Manager help
make vault-help
```

**Important:** After renewing Intermediate CA, you need to:
1. Restart backend: `make restart-backend`
2. Re-register or regenerate certificates for LXD nodes via dashboard

## Service URLs

- Frontend: http://localhost:2025
- Backend: http://localhost:4050  
- API Docs: http://localhost:4050/docs
- Redis: localhost:6381

## Tips

- Use `make help` to see all commands with descriptions
- Most commands support per-service variants (e.g., `restart-backend`)
- Add `CONTAINER_PREFIX=prod` to customize container names
