#!/bin/sh
# Runs once, when the database volume is first created.
# Creates a non-superuser `portfolio` role that owns the `portfolio` database, so the app
# can manage its own tables but nothing else in the cluster. psql reads the password from
# the environment itself, so it never appears on a command line or in the process list.
set -eu

: "${APP_DB_PASSWORD:?set APP_DB_PASSWORD}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\getenv app_password APP_DB_PASSWORD
CREATE ROLE portfolio LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'app_password';
CREATE DATABASE portfolio OWNER portfolio;
REVOKE ALL ON DATABASE portfolio FROM PUBLIC;
-- Other logins have no business in the cluster's own databases.
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname portfolio <<'SQL'
ALTER SCHEMA public OWNER TO portfolio;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
