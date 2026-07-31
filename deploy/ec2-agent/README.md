# EC2 Agent-only test deployment

This package runs only the AI Agent API and its async worker. It does not start
`system_app` or the React frontend.

## Fixed target

- Host: `ec2-user@13.124.53.57`
- ED25519 host key: `SHA256:WO97JHxFDMKxNdldB96LlSZhfae/zg2Wtpqjuje2jbg`
- Public Agent port: `8701`
- Runtime: the host's Python 3.11 and `systemd`
- Existing `chat-server.service` on port `8766` is left untouched.

The host has 2 vCPU and about 912 MiB RAM. Host preparation added a persistent
1 GiB `/swapfile`; about 17 GiB disk remains free. The native Python deployment
avoids a Docker/frontend build on this small test instance. It also created the
persistent Agent directories without installing or starting the Agent.

## External dependencies

Agent-only does not mean dependency-free. Before activation, provide:

1. Agent PostgreSQL runtime and migration URLs.
2. A Backend PostgreSQL read-only URL with the v1.3 `ai_v13_*` views.
3. The existing Backend HTTP base URL.
4. Three distinct service tokens.
5. A 32-byte URL-safe base64 feedback encryption key.
6. Bedrock access through an EC2 IAM role or a Bedrock API key. No IAM instance
   role was attached during the 2026-07-29 preflight.
7. A private HTTPS route to the self-hosted Langfuse EC2 instance and
   project-scoped public/secret keys.
8. Approved input/output token prices for the active Agent model.

The workstation's current database and Backend URLs use `127.0.0.1`; those
values cannot be copied to EC2 unless the matching services also run on EC2.

## Prepare a release locally

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/ec2-agent/prepare_bundle.ps1
```

Bundle creation fails if the Git worktree contains any tracked or untracked
change. The default release ID is `git-<12-character-commit>`, so the same
clean commit produces the same archive bytes and SHA-256. The generated
`release-manifest.json` binds the release ID to the full commit SHA, commit
timestamp, critical source hashes, dependency lock, prompt source, safe model
identifiers, configuration template, Backend read contract source, and
PRO-CTCAE reference.

The archive contains `agent_app`, `shared`, the PRO-CTCAE reference, the tested
runtime dependency lock, and these deployment scripts. It contains only empty
environment examples—no runtime secret, private SSH key, or generated
credential file. Upload both the archive and its adjacent `.sha256` file
without renaming either one. The checksum detects corruption; distribute the
artifact through an authenticated channel because the checksum is not a
release signature. For the first bootstrap, also upload `install_release.sh`
and `verify_release_artifact.py` from that same clean commit; subsequent
installs may use those files from the active verified release.

## Host preflight

Upload only the preflight script or run it after extracting the release:

```bash
AGENT_PORT=8701 bash deploy/ec2-agent/preflight.sh
bash deploy/ec2-agent/prepare_host.sh
AGENT_PORT=8701 bash deploy/ec2-agent/preflight.sh
```

For an upgrade while the existing Agent API is healthy on `8701`, use
`ALLOW_EXISTING_AGENT_UPGRADE=true`. Preflight permits the occupied port only
when `dranswer-agent-api.service` is active and its `MainPID` owns the
listener; the flag cannot waive a collision with an unrelated process.

## Install without starting

```bash
bash deploy/ec2-agent/install_release.sh \
  /home/ec2-user/dranswer-agent-<release>.tar.gz
```

This creates an immutable
`/opt/dranswer-agent/releases/<release>/venv` with the exact dependencies for
that release and creates two environment files on the first run:

- `/etc/dranswer-agent/agent.env`: common Agent configuration, owned by
  `root:ec2-user` with mode `0640`. It must never contain AWS credentials.
- `/etc/dranswer-agent/bedrock.env`: Bedrock bearer scope, owned by `root:root`
  with mode `0600`. Only the API and worker units load it; migration and
  Langfuse exporter units explicitly do not.

Installation does not change `/opt/dranswer-agent/current`, install a systemd
unit, migrate the database, or start a service. Each release also has a
root-owned `release.env`; all Agent units load it after `agent.env`, binding
`APP_RELEASE_VERSION` and `AGENT_RELEASE_COMMIT_SHA` to the verified manifest.

Edit the environment file and replace every `CHANGE_ME` value:

```bash
sudoedit /etc/dranswer-agent/agent.env
sudo -n /opt/dranswer-agent/releases/<release>/venv/bin/python \
  /opt/dranswer-agent/releases/<release>/deploy/ec2-agent/validate_env.py \
  /etc/dranswer-agent/agent.env \
  /etc/dranswer-agent/bedrock.env
```

Set `BEDROCK_AUTH_MODE=bearer_token` to use a Bedrock API key. Install the
token only through stdin; never put its value in an argument, command literal,
or `agent.env`:

```bash
sudo -v
read -rsp 'Bedrock bearer token: ' bedrock_token
printf '\n'
printf '%s\n' "$bedrock_token" |
  bash /opt/dranswer-agent/releases/<release>/deploy/ec2-agent/install_bedrock_token.sh
unset bedrock_token
```

`printf` is a shell builtin, so the secret is sent through stdin without
appearing in child-process argv. The installer suppresses terminal echo,
rejects argument-based input and multiline values, and atomically installs the
root-only file without printing the token.

Alternatively, set `BEDROCK_AUTH_MODE=iam_role`, attach a least-privilege EC2
instance profile, and leave `bedrock.env` empty. Activation performs a real
Bedrock generation probe, so a missing or unauthorized role fails closed.

From Windows, the helper accepts a local file containing either one raw token
or one `AWS_BEARER_TOKEN_BEDROCK=...` assignment and streams it directly to
the remote installer. Host, user, and key path are explicit; the token is not
an argument or output, and the helper uploads no remote staging file. The
remote installer uses only a root-owned `0600` atomic candidate and removes it
on failure:

```powershell
powershell -ExecutionPolicy Bypass `
  -File deploy/ec2-agent/install_bedrock_token_remote.ps1 `
  -HostName 13.124.53.57 `
  -UserName ec2-user `
  -IdentityFile C:\secure\actual-key.pem `
  -ReleaseId <release> `
  -TokenFile C:\secure\bedrock-token.txt
```

The helper enforces `StrictHostKeyChecking=yes`. Before first use, verify the
server's ED25519 fingerprint from a trusted channel and add that verified key
to `known_hosts`; do not use `StrictHostKeyChecking=no` or
`UserKnownHostsFile=/dev/null`. No private key is included in this repository.

Generate suitable secret values without writing them to shell history:

```bash
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

## Activate only after dependencies and the recovery gate are ready

```bash
export AGENT_DB_BACKUP_ID='approved-backup-or-disposable-test-db-id'
export AGENT_SCHEMA_FORWARD_COMPATIBLE=true
bash /opt/dranswer-agent/releases/<release>/deploy/ec2-agent/activate.sh \
  <release>
```

`AGENT_DB_BACKUP_ID` must identify the verified encrypted backup/PITR point.
For an explicitly disposable synthetic test database, record its approved
disposable database ID. `AGENT_SCHEMA_FORWARD_COMPATIBLE=true` is an operator
assertion that the currently running application remains valid after every
forward migration in the candidate release. Do not set it for a migration
that removes or changes data needed by the current release.

Activation is serialized with installation. It validates the installed
manifest, release-specific venv, release metadata, both environment scopes,
and then runs migration and a real Bedrock generation canary from the
candidate release on loopback port `18701` while the previous release remains
active. Only after that canary passes does it stop the prior Agent services,
atomically switch `current`, install Agent-only systemd units, and start the
new API and worker. The Langfuse exporter is enabled only when configured.
The migration unit remains static and dependency-driven.

Final verification requires:

- API and worker enabled for reboot recovery;
- optional exporter enabled exactly when configured;
- current symlink, manifest, `APP_RELEASE_VERSION`, commit SHA, and venv agree;
- all relevant Agent units active;
- liveness, readiness, authenticated real generation, a running worker
  heartbeat, and non-critical Agent ops readiness.

Agent ops status `degraded` is accepted only when at least one worker is
currently running. Status `critical` always fails activation. This keeps stale
historical worker rows from blocking a healthy upgrade without masking dead
tasks, callback failures, Backend-read failure, or a missing live worker.

On any activation error or signal after the switch starts, the script restores
the previous symlink, previous unit files, enable state, and running Agent
services. It does **not** downgrade the database. The preserved `previous`
symlink and release-specific venv make application dependency restoration
exact, but rollback is safe only while the new schema remains compatible.

Explicit application rollback:

```bash
export AGENT_DB_BACKUP_ID='the-retained-backup-or-PITR-id'
export AGENT_SCHEMA_ROLLBACK_COMPATIBLE=true
bash /opt/dranswer-agent/current/deploy/ec2-agent/rollback.sh
```

Pass a specific installed release ID as the final argument to override the
preserved `previous` target. A database/schema rollback is a separate,
maintenance-mode PostgreSQL snapshot/PITR procedure; this script never
pretends to reverse DDL or data changes.

The generation and ops probes read service tokens inside Python processes;
tokens are never placed in curl argv or printed. After activation, perform an
EC2 reboot drill and rerun `verify.sh` before admitting traffic.

Useful diagnostics:

```bash
sudo journalctl -u dranswer-agent-api -u dranswer-agent-worker \
  -u dranswer-agent-langfuse-exporter -n 200 --no-pager
sudo systemctl status dranswer-agent-api dranswer-agent-worker \
  dranswer-agent-langfuse-exporter
curl -fsS http://127.0.0.1:8701/health
curl -fsS http://127.0.0.1:8701/health/ready
bash /opt/dranswer-agent/current/deploy/ec2-agent/verify.sh
```

Do not diagnose this deployment with `cat bedrock.env`,
`systemctl show -p Environment`, `ps e`, `/proc/<pid>/environ`, or `set -x`;
those commands can disclose credentials.

For the EC2 security group, limit source CIDRs for `8701/tcp` even though the
available test range is `8700-8799`. Direct HTTP on `8701` is only for a
source-restricted test network; put TLS termination in front of the service
before sending bearer tokens over an untrusted network.

Do not expose the Langfuse UI or ingestion endpoint publicly. Allow the Agent
EC2 security group to reach the private Langfuse HTTPS endpoint, and restrict
the UI to the administrator network. The exporter reads only the PHI-free
outbox and never runs in the Agent API request path.
