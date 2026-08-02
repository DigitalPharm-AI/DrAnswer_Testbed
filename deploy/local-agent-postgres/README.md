# Local Agent pgvector PostgreSQL

The Backend/System database remains on the native PostgreSQL instance at port
`55432`. The Agent database runs separately on `127.0.0.1:55433` so the Agent
can use the `vector` extension without changing the Backend database runtime.

The local testbed intentionally uses PostgreSQL trust authentication and binds
the container port only to `127.0.0.1`. Do not reuse this authentication setup
for EC2 or other shared environments.

Start the database:

```powershell
docker compose -f docker-compose.agent-postgres.yml up -d
```

Stop the database without deleting its named volume:

```powershell
docker compose -f docker-compose.agent-postgres.yml stop
```

The persistent volume is named `dranswer-agent-postgres-data`. A normal
`docker compose down` keeps this volume. Do not use `down --volumes` unless the
database has been backed up and deletion is explicitly intended.

After importing the MFDS reference data and applying Agent migrations, build
or resume the Cohere reference embeddings with:

```powershell
$env:DA_DRUG_SERVICE = "agent_app"
$env:DA_DRUG_ENV_FILE = ".env.9000,.env.agent_app.secret"
.\.venv\Scripts\python.exe -m scripts.backfill_agent_embeddings --target all
```

The command is resume-safe. Confirm that no current rows are missing without
calling Bedrock by running the same command with `--check`. It exits non-zero
when either target has pending rows.
