#!/bin/sh
# Run on the Docker host with its existing private .env and backup directory.
set -eu
# Refuse dangerous host binding before Docker publishes anything.
# The container also checks this value before starting the web server.
docker compose run --rm --no-deps --entrypoint python web -c '
import os
from portfolio_digest.web.binding import private_host
private_host(os.environ.get("DASHBOARD_BIND", "127.0.0.1"))
'
docker compose run --rm backup --once
docker compose up -d --build web
