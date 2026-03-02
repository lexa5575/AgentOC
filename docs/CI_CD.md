# CI/CD Setup

## CI workflow

File: `.github/workflows/ci.yml`

Runs on:
- push to `main` / `master`
- pull request
- manual trigger

Steps:
1. Build `agentos-api` Docker image.
2. Start `agentos-db` + `agentos-api`.
3. Run tests: `python -m pytest -q tests`.
4. Run compile check: `python -m compileall -q ...`.
5. Always stop containers.

## CD workflow

File: `.github/workflows/cd.yml`

Runs on:
- manual trigger (`workflow_dispatch`)

Inputs:
- `ref` (branch/tag/sha to deploy)
- `run_migrations` (`true`/`false`)

### Required repository secrets

Set these in GitHub repo settings:
- `DEPLOY_HOST` (server hostname/IP)
- `DEPLOY_USER` (ssh user)
- `DEPLOY_SSH_KEY` (private key in PEM/OpenSSH format)
- `DEPLOY_PATH` (absolute path to project on server)
- `DEPLOY_PORT` (optional, default `22`)

### Deploy behavior

The workflow connects to the server over SSH and runs:
1. `git fetch`, `git checkout <ref>`, `git pull` (for branch refs).
2. `docker compose -f compose.prod.yaml build agentos-api`
3. `docker compose -f compose.prod.yaml up -d agentos-db agentos-api`
4. Optional migration scripts:
   - `scripts/migrate_thread_id.py`
   - `scripts/migrate_conversation_states.py`
   - `scripts/migrate_client_profile.py`

## Suggested branch protection

For `main`:
1. Require pull request before merge.
2. Require status check: `Build And Test (Docker)`.
3. Require branches to be up to date before merge.
