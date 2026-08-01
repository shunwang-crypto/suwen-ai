#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"

# The official postgres image runs this file only on a new data directory.
# The migration role is the database owner; the application role is deliberately
# NOSUPERUSER/NOBYPASSRLS and receives only the grants from Alembic migrations.
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_password="$POSTGRES_APP_PASSWORD" <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'edu_app') THEN
        CREATE ROLE edu_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;
ALTER ROLE edu_app WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password';
SQL
