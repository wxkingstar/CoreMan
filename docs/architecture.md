# Architecture

CoreMan separates chat delivery, task execution and administration into independently running services.

- **Management API**: FastAPI serves the Vue interface and authenticated administration endpoints.
- **PostgreSQL**: stores configuration, leases, task state, event streams and a durable outbox. LISTEN/NOTIFY wakes consumers, with polling for recovery.
- **Gateways**: maintain platform connections, normalize inbound events and deliver queued responses.
- **Workers**: resolve the current requester, construct prompts, call an assigned runtime and persist results.
- **Scheduler**: creates scheduled work, reclaims interrupted tasks and coordinates maintenance.
- **Runtime Daemon**: connects outward to the management service and supervises local CLI drivers. The daemon user and host permissions define the execution boundary.
- **Storage**: attachments use a shared local volume or an explicitly configured S3 backend.

The services coordinate through persisted state rather than an additional message broker. Multiple workers and gateways use database claims and leases to avoid concurrent ownership.

Secrets remain in deployment configuration or encrypted database fields. Do not expose database, monitoring or driver ports as public application endpoints. Deploy independent runtime user environments when workloads require separation.

For installation and upgrades, see [operations](operations.md) and [Runtime Daemon](../runtime_daemon/README.md).
