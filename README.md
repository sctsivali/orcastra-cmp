# Orcastra Dashboard

## Stack

- **Backend**: FastAPI, Python
- **Frontend**: Next.js, React, TailwindCSS
- **Deployment**: Docker

## Prerequisites

- Docker
- LXD/MicroCloud nodes with API access enabled
- Valid client certificates for each node

## Architecture

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

- `/instances` - Instance management
- `/images` - Image operations
- `/networks` - Network configuration
- `/storage-pools` - Storage management
- `/projects` - Project operations
- `/profiles` - Profile management
- `/nodes` - Node information
- `/ipam` - IP address management

See `/docs` for interactive API documentation.

## Versioning

Project follows [Semantic Versioning](https://semver.org/) (SemVer):

- **MAJOR** (x.0.0) - Breaking changes that are incompatible with previous versions
- **MINOR** (0.x.0) - New features that maintain backward compatibility
- **PATCH** (0.0.x) - Bug fixes and minor changes without new features

When updating versions, modify the version field in:
- `backend/app/core/config.py` - API version
- `frontend/package.json` - Frontend version
