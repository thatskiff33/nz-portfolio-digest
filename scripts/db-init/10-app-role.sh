#!/bin/sh
# Runs once, when the database volume is first created.
# Creates a non-superuser `portfolio` role that owns the `portfolio` database, so the app
# can manage its own tables but nothing else in the cluster.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE portfolio LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
CREATE DATABASE portfolio OWNER portfolio;
REVOKE ALL ON DATABASE portfolio FROM PUBLIC;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname portfolio <<'SQL'
ALTER SCHEMA public OWNER TO portfolio;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
