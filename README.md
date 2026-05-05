# Orcastra Dashboard

Control plane for managing LXD/Incus hypervisors across multiple nodes and organizations. Supports multi-cluster orchestration, real-time monitoring, RBAC, and centralized logging.

## Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | FastAPI, Python 3.11+, SQLAlchemy (async), Alembic |
| **Frontend** | Next.js 15, React 18, TailwindCSS, Radix UI |
| **Database** | PostgreSQL 17 |
| **Cache** | Redis 8 (encrypted, LRU eviction) |
| **Auth** | Authentik (OAuth2/OIDC), NextAuth.js 5, JWT |
| **Secrets** | HashiCorp Vault (PKI, cluster certificates) |
| **Logging** | Fluent Bit -> OpenSearch (centralized log aggregation) |
| **Deployment** | Docker (dev + prod) |

## Features

### Virtualization Management
- Multi-node LXD/Incus/MicroCloud cluster support
- Instance lifecycle (create, start, stop, restart, delete VMs and containers)
- Image management, project isolation, profile templates
- Storage pool and volume operations
- Virtual network management with ACLs and IPAM

### Interactive Console & Terminal
- Full terminal access over WebSocket (xterm.js)
- SPICE graphics console for VGA/remote desktop
- Session management (list, close all, show all)
- Session recording and download
- Drag-and-drop file upload to instances
- Resource info overlay while console is running

### Security & Access Control
- OAuth2/OIDC SSO via Authentik
- JWT token validation on backend
- 3-tier RBAC (Admin / Partner / Tenant) integrated with Authentik user groups
- Fine-grained per-resource permissions (create, read, update, delete)
- Tenant access policies (cluster/project/instance ACLs)
- Read-only demo mode
- Rate limiting, encrypted Redis cache, HMAC-SHA256 API keys

### Multi-Cluster & Region Management
- Vault-based cluster registry with dynamic discovery
- Region/geographic grouping with cluster zone and hardware info
- Cluster health monitoring and automatic certificate refresh
- Cluster/facility registration

### Monitoring & Observability
- Real-time CPU, memory, disk, network metrics (WebSocket)
- Historical metrics stored in PostgreSQL (time-series snapshots)
- Warnings and alerts from LXD with redirection links
- Activity audit log with action, result, user tracking
- Session tracker for terminal and console access

### Logging & Compliance
- Fluent Bit log collector (sidecar) forwarding to OpenSearch
- Centralized log storage with index lifecycle management
- Log archival with ISM policies and snapshot scheduling
- OpenSearch dashboards for access logs, audit logs, and overview

### Personalization & Branding
- Per-user theme preference (light/dark) synced to database
- Custom accent colors, sidebar, logo upload (light + dark)
- Organization-level branding (app name, tagline, footer)
- Undo/redo support in personalization settings

### Integration & External APIs
- API key management (create, revoke, expire)
- OrcaHub integration for external orchestration
- Raw HTTP proxy to LXD for third-party tools
- User mapping between external systems and Orcastra

## Prerequisites

- Docker
- LXD/Incus/MicroCloud nodes with API access enabled
- Valid client certificates for each node
- HashiCorp Vault (secrets and certificate management)
- Authentik (SSO/identity provider)
- OpenSearch (optional, for centralized logging)

## Architecture

```
+-------------------------------------------------------------------+
|                       ORCASTRA DASHBOARD                          |
+-------------------------------------------------------------------+
|                                                                   |
|   +-----------+     +------------+     +----------------------+   |
|   | Frontend  |---->|  Backend   |---->|     PostgreSQL       |   |
|   | (Next.js) |     | (FastAPI)  |     |                      |   |
|   +-----------+     +-----+------+     +----------------------+   |
|                           |                                       |
|                           v                                       |
|                     +-----------+                                 |
|                     |   Redis   | <-- Caching (LXD responses,     |
|                     |  (Cache)  |     API results, sessions)      |
|                     +-----------+                                 |
|                           |                                       |
|                           v                                       |
|                     +------------+     +----------------------+   |
|                     | Fluent Bit |---->|     OpenSearch       |   |
|                     |(Log Router)|     | (Log Storage/Search) |   |
|                     +------------+     +----------+-----------+   |
|                                                  |                |
|                                                  v                |
|                                       +----------------------+    |
|                                       | OpenSearch Dashboards|    |
|                                       |   (Visualization)    |    |
|                                       +----------------------+    |
+-------------------------------------------------------------------+
```

### Ports

| Environment | Frontend | Backend |
|-------------|----------|---------|
| Development | 2025 | 4050 |
| Production  | 4321 | 8765 |

## API Endpoints

<details>
<summary>Show API endpoints</summary>

All endpoints prefixed with `/api/v1`:

### Core Operations
- `/instances` – Container/VM lifecycle management
- `/images` – Image import, list, delete
- `/projects` – Project isolation and management
- `/profiles` – Instance configuration templates
- `/networks` - Virtual network management
- `/network-acls` - Network access control rules
- `/storage-pools` – Storage pool provisioning
- `/storage-volumes` – Volume operations and snapshots
- `/nodes` – Node health and resources
- `/cluster-members` – MicroCloud cluster members
- `/cluster-registration` – Register new clusters/facilities

### Interactive Access
- `/terminal` – WebSocket terminal sessions (create, list, close, history)
- `/console` – SPICE graphics console (create, capabilities, audit logs)
- `/file-transfer` – Upload files to instances

### Monitoring & Observability
- `/monitoring` – Real-time metrics, history, WebSocket live feed
- `/regions` – Geographic region and cluster grouping
- `/lxd-warnings` – Cluster warnings and alerts
- `/lxd-operations` – Async operation polling
- `/lxd-server` – Server metadata and resources
- `/archive` – OpenSearch log archive management
- `/activity` – Audit trail logs
- `/tags` – Instance tagging

### User & Access Management
- `/permissions` – LXD identity and group management
- `/my-keys` – Personal TLS key generation
- `/user-preferences` – Theme, branding, personalization
- `/organizations` – Multi-tenant organization management
- `/user-groups` – Admin user group assignment
- `/tenant-access` – ACL policies and rules

### Integration
- `/integrations` – API key management, OrcaHub sync
- `/external-proxy` – Raw HTTP proxy to LXD

### System
- `/system` – System info and cache control
- `/ipam` – IP address management
- `/health` – Health check

See `/docs` for interactive Swagger API documentation.

</details>

## Quick Start

<details>
<summary>Show quick start steps</summary>

```bash
# clone and configure
git clone https://github.com/sctsivali/orcastra-dashboard.git
cd orcastra-dashboard
cp .env.example .env
# edit .env with your values (vault, authentik, database, etc.)

# build and start
make build
make up

# check status
make status
make health
```

</details>

## Make Commands

<details>
<summary>Show make commands</summary>

```bash
# build
make build              # build all Docker images
make build-nc           # build without cache
make build-backend      # build backend only
make build-frontend     # build frontend only

# lifecycle
make up                 # start all services
make down               # stop all services
make restart             # restart all services
make restart-backend    # restart backend only

# diagnostics
make logs               # view all logs
make logs-backend       # view backend logs
make logs-frontend      # view frontend logs
make logs-redis         # view redis logs
make ps                 # list running containers
make status             # show service status
make health             # check service health

# development
make dev-backend        # run backend locally
make dev-frontend       # run frontend locally
make shell-backend      # exec into backend container
make shell-frontend     # exec into frontend container

# version management
make version            # show current version
make version-set VERSION=x.x.x  # set and sync version
make version-sync       # sync version across all files
make version-check      # check version consistency

# data
make backup             # backup database
make restore BACKUP_FILE=path  # restore database
make redis-cli          # open redis CLI

# deployment
make deploy-package     # create tar.gz for on-prem deployment
make config             # validate docker-compose configuration
make env-check          # verify .env file exists
```

</details>

## Configuration

<details>
<summary>Show configuration example</summary>

Copy `.env.example` to `.env` and configure:

```bash
# version
APP_VERSION=xxx

# ports
FRONTEND_PORT=xxx      # dev: 2025, prod: 4321
BACKEND_PORT=xxx       # dev: 4050, prod: 8765

# database
POSTGRES_USER=orcastra
POSTGRES_PASSWORD=your-secure-password
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/orcastra_dashboard

# vault
VAULT_ENABLED=true
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=your-vault-token

# redis
REDIS_ENABLED=true
REDIS_ENCRYPTION_ENABLED=true
REDIS_ENCRYPTION_KEY=your-fernet-key       # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# authentik (OAuth2/OIDC)
AUTHENTIK_ISSUER=https://auth.example.com/application/o/orcastra-dashboard/
AUTHENTIK_CLIENT_ID=your-client-id
AUTHENTIK_CLIENT_SECRET=your-client-secret
AUTHENTIK_API_URL=https://auth.example.com
AUTHENTIK_API_TOKEN=your-admin-token

# nextauth
NEXTAUTH_URL=http://your-server:2025
NEXTAUTH_SECRET=your-nextauth-secret       # openssl rand -base64 32

# opensearch (logging)
OPENSEARCH_HOST=your-opensearch-host
OPENSEARCH_PORT=9200
OPENSEARCH_USER=fluentbit
OPENSEARCH_PASSWORD=your-opensearch-password

# security
CORS_ORIGINS=http://your-server:2025
RATE_LIMIT_ENABLED=true
SECRET_KEY=your-secret-key                 # openssl rand -hex 32
READ_ONLY_MODE=false
```

</details>

## Versioning

Project follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (x.0.0) – Breaking changes incompatible with previous versions
- **MINOR** (0.x.0) – New features with backward compatibility
- **PATCH** (0.0.x) – Bug fixes without new features
- **RC** (x.x.x-RCn) – Release candidate for testing before stable release

Version is centralized in `.env` (`APP_VERSION`) and propagated to all components via `make version-sync`.

## Key Resources

- License: GNU GPL v3 ([LICENSE](LICENSE))
- Contribution workflow: [CONTRIBUTING.md](CONTRIBUTING.md)
- Community standards: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Security disclosure: [SECURITY.md](SECURITY.md)
- Support channels: [SUPPORT.md](SUPPORT.md)
- Product and deployment docs: https://docs.orcastra.io
- Community forum: https://discourse.orca.id
