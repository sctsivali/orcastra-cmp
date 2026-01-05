# Orcastra Dashboard

## Stack

- **Backend**: FastAPI, Python 3.11+
- **Frontend**: Next.js 15, React 18, TailwindCSS
- **Cache**: Redis
- **Secrets**: HashiCorp Vault
- **Deployment**: Docker

## Prerequisites

- Docker
- LXD/MicroCloud nodes with API access enabled
- Valid client certificates for each node
- Redis (included in docker compose)
- HashiCorp Vault (for secrets management)

## Ports

Backend aggregates data from configured nodes via their REST APIs. Frontend communicates exclusively with backend.

### Dev
```
frontend:2025 → backend:4050 → nodes
```

### Prod
```
frontend:4321 → backend:8765 → nodes
```

## API Endpoints

All endpoints prefixed with `/api/v1`:

- `/instances` - Container/VM management
- `/images` - Image operations
- `/networks` - Network configuration
- `/network-acls` - Network ACL rules
- `/storage-pools` - Storage pool management
- `/storage-volumes` - Volume operations 
- `/projects` - Project management
- `/profiles` - Profile configuration
- `/nodes` - Node informaon
- `/cluster-members` - MicroCloud cluster members
- `/monitoring` - System metrics
- `/my-keys` - Certificate generation
- `/permissions` - Access control
- `/groups` - Group management
- `/identities` - Identity management
- `/ipam` - IP address management
- `/system` - System info & cache control

See `/docs` for interactive API documentation.

## Make Commands

```bash
make help          # Show all available commands
make build         # Build Docker images
make up            # Start all services
make down          # Stop all services
make logs          # View all logs
make logs-backend  # View backend logs
make restart       # Restart all services
make clean         # Remove containers and volumes
```

## Configuration

Environment variables (`.env`):

```bash
# App
APP_VERSION=x.x.x
DEBUG=false

# Ports
FRONTEND_PORT=2025-or-4321
BACKEND_PORT=4050-or-8765
REDIS_PORT=6381

# Redis
REDIS_ENABLED=true
REDIS_ENCRYPTION_ENABLED=true

# Vault
VAULT_ENABLED=enable
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=your-token

# Dashboard Settings
READ_ONLY_MODE=false # set true for view only mode/no access for create or edit
```

## Versioning

Project follows [Semantic Versioning](https://semver.org/) (SemVer):

- **MAJOR** (x.0.0) - Breaking changes that are incompatible with previous versions
- **MINOR** (0.x.0) - New features that maintain backward compatibility
- **PATCH** (0.0.x) - Bug fixes and minor changes without new features

Version is set via `APP_VERSION` in `.env` and propagates to all components.
