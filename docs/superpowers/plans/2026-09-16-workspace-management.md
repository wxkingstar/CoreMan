# Workspace management implementation plan

> **For agentic workers:** Use subagent-driven-development to implement and review bounded tasks.

**Goal:** Let nontechnical employee administrators browse/edit runtime files, back up to Git, initialize instructions and safely migrate files plus memory.
**Architecture:** Authenticated API mediates bounded runtime operations; workspace configuration and operation progress persist per employee. Runtime file transfers are chunked, staged and validated. The existing switch service changes binding only after workspace and memory preparation.
**Tech Stack:** Python FastAPI / SQLAlchemy / runtime daemon, Vue TypeScript, PostgreSQL.
**Spec:** docs/superpowers/specs/2026-09-16-workspace-management.html (approved).

## Global constraints
- Default new runtime root /home/ai; retain existing configured roots.
- AGENTS.md canonical; CLAUDE.md relative symlink to AGENTS.md; preserve conflicting instructions.
- Employee-admin file permissions; authenticated reverse channel; no arbitrary shell or filesystem access.
- Never force push, overwrite unknown nonempty directories, or claim backup before push succeeds.
- Migration must stop new execution, preserve source on failure, and persist recovered memory independently.
- UI uses Chinese operational wording and scrollable panels with fixed actions.

## Task 1: Runtime workspace operations
Files: runtime_daemon/workspace.py (new), runtime_daemon/agent.py, runtime_daemon/build.py, tests/unit/test_runtime_workspace.py.
- [x] Add failing real filesystem tests for initialization, links, containment, read/write hash conflicts, export/import hashes, Git status and push.
- [x] Implement dispatch operations workspace-info/init/list/read/write, workspace-export/import/finish, workspace-git-status/test/backup/restore.
- [x] Read responses are bounded; transfer uses 256KiB base64 chunks; importing only to operation-owned staging directory; finish verifies manifest then atomically installs.
- [x] Run runtime unit suite and inspect bundle inclusion.

## Task 2: Workspace API and persisted configuration
Files: coreman/api/routers/bot_workspace.py, coreman/core/db/models/bots.py, migrations/versions/0028_workspace_management.py, tests/api/test_bot_workspace.py, coreman/api/main.py.
- [x] Tests: permissions, runtime unavailability, safe error reporting, writes during active tasks, encrypted Git token, push result state.
- [x] Add Bot workspace state/config columns and per-employee browse/read/write/Git config/status/backup endpoints; shared operation guard locks employee row, checks active tasks and migration state.
- [x] Register routes and runtime operation allowlist; use current root and working_dir from database, never caller-controlled root.

## Task 3: Migration and initialization integration
Files: coreman/core/bots/workspace.py, workspace_transfer.py (new), switch_relay.py, coreman/core/knowledge/memory_transfer.py, coreman/api/routers/bots.py, bots_extra.py, coreman/core/bus/tasks.py, tests/api/test_memory_transfer.py and new tests.
- [x] Tests first: creation initializes or stays pending; source snapshot committed before failure; target existing directory rejected; queued claims fenced while migration runs.
- [x] Introduce persisted operation state; task claim synchronizes with bot row lock so checking active tasks and fencing is race-free.
- [x] Implement copy/git/existing choices, offline stored-memory explicit consent, snapshot before file transfer, deployment before binding, retry/error progress.
- [x] Creation initializes before executable; unknown old daemon capability errors explain upgrade requirement.

## Task 4: UI and integration
Files: web/src/components/WorkspaceDrawer.vue, api/workspace.ts, existing detail/form/switch components and runtime defaults; web tests.
- [x] Add tested file drawer, text editing with conflict handling and download; Git settings/status/backup.
- [x] Path-adjacent entry; switching source/target directory and stored-memory consent; operation progress and retry initialization.
- [x] Build, lint targeted files, component tests; broader Python/runtime regression and independent final review.

## Contracts
Agent payload always includes working_dir and bot_id from trusted API. init additionally content. list uses path; read path/offset/length returns content (text when small UTF8), data (base64), size, hash, editable; write path/content/expected_hash returns hash. info returns exists, empty, owned, workspace_protocol=1. All results success=true. Git operations accept git_url, branch, git_access_token, files, message; status returns initialized, branch, files[{path,status}], head. export returns manifest with path/size/hash/mode/link, excluded; transfer reads via workspace-read binary chunks; import receives transfer_id, path, data, offset, manifest on first call; finish transfer_id/manifest/bot_id/content. Precise runtime contracts will be documented by implementer and reconciled before integration.

API base /api/admin/bots/{id}/workspace. GET '' returns directory, state, error, git_url, branch, has_token, last_backup_at. GET /files?path=; GET /file?path=&offset=&length=; PUT /file {path,content,expected_hash}; PUT /git {git_url,branch,access_token?}; GET /git/status; POST /git/test; POST /git/backup {files,message}; POST /initialize. Switch input extends workspace_mode: copy|git|existing (default copy), target_directory optional, allow_stored_memory false. Existing client backwards compatibility preserved through defaults, but missing capabilities fail explicitly.
