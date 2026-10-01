#!/bin/bash
# Runs once on first container start (docker-entrypoint-initdb.d).
# Creates the least-privilege application role used by all services; migrations (run as the
# owner role) grant it table privileges and RLS applies to it.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE EXTENSION IF NOT EXISTS vector;
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
      CREATE ROLE app_user LOGIN PASSWORD '${APP_DB_PASSWORD:-app_password}';
    END IF;
  END \$\$;
SQL
