# Business token providers implementation plan

> Execute with subagent-driven-development; user has approved the provider design and requested implementation plus a PR, not deployment.

Goal: retain CoreMan's default self/external-key signing while allowing each business system to select a deployment-configured HTTP delegated-token provider. No company-specific addresses or secrets in source.

## Contract and rulings

- Deployment `BUSINESS_TOKEN_PROVIDERS` is a JSON mapping of named HTTP providers; `builtin` is reserved. Each has HTTPS `token_url`, `client_id`, SecretStr `client_secret`, optional positive `max_token_ttl_seconds`; configurable request field names `subject_field` (username), `audience_field` (audience), `ttl_field` (expires_in). Only form POST + OAuth Basic authentication; trusted username assertion protocol, not generic RFC8693 token exchange.
- System fields: `token_provider` default builtin, `token_audience` optional override of system key, `access_test_url` optional URL to a protected API on the same origin as base_url. External-provider testing requires this URL. Migration 0057 is additive. Old clients omitting new fields on PUT must preserve existing external configuration.
- External provider TTL=min(task timeout, configured ceiling) or task timeout when absent. No +300 seconds. Builtin signing also follows task timeout without the former +300; preserve its key/claims/cookie behavior. Probe requests ask for at most 60 seconds. Validate actual returned expires_in is positive and <= requested; do not assume configured expiry was granted.
- Retain trusted speaker -> token_subject and bot system allowlists. Do not read identity from model tool arguments or bot env. Provider failures suppress only affected system credentials with generic actionable messages, never fallback or log bodies/secrets. Unknown provider fails closed after removal from deployment.
- HTTP client: no redirects, HTTPS endpoint without userinfo/query/fragment, no ambient proxy/env authentication, bounded timeout/response size, safe sanitized errors. No SSO secret in runtime environment. Returned JWT remains a per-turn runtime credential, covered by existing redaction. No new request proxy/broker or automatic refresh of a running subprocess in this PR; expired credentials require a fresh trusted task/turn.
- All dispatch points use the same provider selection: chat, cron, health probe, administrator system test. Preserve cookie-based builtin testing and use Bearer + protected nonredirecting API tests for external providers.

## Work

- [x] Provider tests and config/HTTP adapter: unit tests first, show failure, implement validated config + TokenRequest/IssuedToken + async HTTP provider.
- [x] System settings: migration/model/API/UI with safe provider metadata endpoint (names/cap only, no URL/client/secret); unknown provider and invalid origin rejected; API/UI regression tests.
- [x] Integrate runtime and test-access: behavior tests first; inject providers through worker base dependencies; cap TTL, preserve roles/actor payload untouched, report errors without token values; record returned expiry in COREMAN_SYSTEMS. Cover actual conversations/cron through existing tests and updated paths.
- [x] Docs and tests: document neutral external issuer example plus trusted-subject form contract; changelog; unit/integration/API tests, ruff, mypy, frontend checks/build; independent review.
- [ ] Commit only this worktree, push codex/business-token-providers, create and attach PR to main. Do not merge/deploy or modify original user checkout.

## Verification

- Full Python suite: 2,909 passed, 2 skipped; final provider/worker/health regressions: 38 passed.
- Ruff and mypy: passed. Frontend: ESLint, TypeScript, 308 tests, production build passed.
- Independent review: reserved audience and short-token redaction covered; total issuance timeout and health task TTL corrected.
