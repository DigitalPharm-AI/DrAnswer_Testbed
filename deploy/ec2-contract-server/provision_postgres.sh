#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

postgres_unit="postgresql.service"
pg_hba="/var/lib/pgsql/data/pg_hba.conf"
env_dir="/etc/dranswer-agent-contract"
runtime_env="$env_dir/contract.env"
migration_env="$env_dir/contract-migration.env"
service_group="dranswer-contract"
database_name="dranswer_contract"
runtime_role="contract_app_rw"
migration_role="contract_migration"

for command_name in openssl psql systemctl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
done
if ! systemctl is-active --quiet "$postgres_unit"; then
  echo "PostgreSQL service is not active." >&2
  exit 1
fi
if [[ ! -f "$pg_hba" ]]; then
  echo "PostgreSQL pg_hba.conf is missing." >&2
  exit 1
fi
if [[ ! -f "$runtime_env" ]]; then
  echo "Contract runtime environment is missing." >&2
  exit 1
fi
if ! getent group "$service_group" >/dev/null 2>&1; then
  echo "Contract service group is missing." >&2
  exit 1
fi

role_count="$(
  sudo -u postgres psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --command "
      SELECT COUNT(*)
      FROM pg_roles
      WHERE rolname IN ('$runtime_role', '$migration_role')
    "
)"
database_count="$(
  sudo -u postgres psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --command "
      SELECT COUNT(*)
      FROM pg_database
      WHERE datname = '$database_name'
    "
)"
if [[ "$role_count" != "0" || "$database_count" != "0" ]]; then
  echo "Refusing to reuse an existing contract database or role." >&2
  exit 1
fi

umask 077
runtime_password="$(openssl rand -hex 32)"
migration_password="$(openssl rand -hex 32)"
if [[ "$runtime_password" == "$migration_password" ]]; then
  echo "Generated PostgreSQL passwords unexpectedly match." >&2
  exit 1
fi

update_env_value() {
  local file_path="$1"
  local key="$2"
  local value="$3"
  local file_owner="$4"
  local file_group="$5"
  local file_mode="$6"
  local temporary_file
  local found=0
  local line

  temporary_file="$(mktemp "$env_dir/.env-update.XXXXXX")"
  if [[ -f "$file_path" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "$key="* ]]; then
        printf '%s=%s\n' "$key" "$value" >>"$temporary_file"
        found=1
      else
        printf '%s\n' "$line" >>"$temporary_file"
      fi
    done <"$file_path"
  fi
  if (( found == 0 )); then
    printf '%s=%s\n' "$key" "$value" >>"$temporary_file"
  fi
  install \
    -m "$file_mode" \
    -o "$file_owner" \
    -g "$file_group" \
    "$temporary_file" \
    "$file_path"
  rm -f "$temporary_file"
}

runtime_url="postgresql+psycopg://${runtime_role}:${runtime_password}@127.0.0.1:5432/${database_name}"
migration_url="postgresql+psycopg://${migration_role}:${migration_password}@127.0.0.1:5432/${database_name}"
update_env_value \
  "$runtime_env" \
  "CONTRACT_DATABASE_URL" \
  "$runtime_url" \
  root \
  "$service_group" \
  0640
update_env_value \
  "$runtime_env" \
  "CONTRACT_STARTUP_MIGRATIONS_ENABLED" \
  "false" \
  root \
  "$service_group" \
  0640
update_env_value \
  "$runtime_env" \
  "CONTRACT_DB_POOL_SIZE" \
  "2" \
  root \
  "$service_group" \
  0640
update_env_value \
  "$runtime_env" \
  "CONTRACT_DB_MAX_OVERFLOW" \
  "1" \
  root \
  "$service_group" \
  0640
update_env_value \
  "$migration_env" \
  "CONTRACT_MIGRATION_DATABASE_URL" \
  "$migration_url" \
  root \
  root \
  0600

{
  printf "CREATE ROLE %s LOGIN PASSWORD '%s';\n" \
    "$migration_role" \
    "$migration_password"
  printf "CREATE ROLE %s LOGIN PASSWORD '%s';\n" \
    "$runtime_role" \
    "$runtime_password"
  printf "CREATE DATABASE %s OWNER %s;\n" \
    "$database_name" \
    "$migration_role"
} |
  sudo -u postgres psql \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --dbname=postgres

{
  printf "REVOKE ALL ON DATABASE %s FROM PUBLIC;\n" "$database_name"
  printf "GRANT CONNECT ON DATABASE %s TO %s, %s;\n" \
    "$database_name" \
    "$migration_role" \
    "$runtime_role"
  printf "ALTER SCHEMA public OWNER TO %s;\n" "$migration_role"
  printf "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
  printf "GRANT USAGE, CREATE ON SCHEMA public TO %s;\n" \
    "$migration_role"
  printf "GRANT USAGE ON SCHEMA public TO %s;\n" "$runtime_role"
} |
  sudo -u postgres psql \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --dbname="$database_name"

hba_backup="${pg_hba}.pre-contract-postgres"
if [[ -e "$hba_backup" ]]; then
  echo "PostgreSQL pg_hba backup already exists." >&2
  exit 1
fi
install \
  -m 0600 \
  -o postgres \
  -g postgres \
  "$pg_hba" \
  "$hba_backup"
hba_temporary="$(mktemp /var/lib/pgsql/data/.pg_hba.XXXXXX)"
{
  printf "host %s %s 127.0.0.1/32 scram-sha-256\n" \
    "$database_name" \
    "$migration_role"
  printf "host %s %s 127.0.0.1/32 scram-sha-256\n" \
    "$database_name" \
    "$runtime_role"
  cat "$pg_hba"
} >"$hba_temporary"
install \
  -m 0600 \
  -o postgres \
  -g postgres \
  "$hba_temporary" \
  "$pg_hba"
rm -f "$hba_temporary"
systemctl reload "$postgres_unit"

PGPASSWORD="$migration_password" psql \
  --no-psqlrc \
  --host=127.0.0.1 \
  --username="$migration_role" \
  --dbname="$database_name" \
  --tuples-only \
  --no-align \
  --command "SELECT current_user, current_database()" >/dev/null
PGPASSWORD="$runtime_password" psql \
  --no-psqlrc \
  --host=127.0.0.1 \
  --username="$runtime_role" \
  --dbname="$database_name" \
  --tuples-only \
  --no-align \
  --command "SELECT current_user, current_database()" >/dev/null

echo "Provisioned fresh local PostgreSQL contract database."
echo "Runtime and migration URLs were written to separate environment files."
echo "No application service was restarted."
