# Deploying on hartleysmithubuntu

The app runs as its own Docker Compose project, `portfolio-digest`, with its own Postgres
container. It is fully separate from the Tokoroa booking site and the server's main
Postgres 18. No ports are published: nothing here is reachable from outside the server.

## First install

```sh
git clone https://github.com/thatskiff33/nz-portfolio-digest.git ~/portfolio-digest
cd ~/portfolio-digest
cp .env.example .env
chmod 600 .env
# Fill in both passwords with: openssl rand -hex 32
nano .env

docker compose up -d db backup
docker compose run --rm --build migrate          # creates the tables
docker compose run --rm migrate health           # should print "OK: ..."
docker compose run --rm migrate portfolio add "Jubbles Enterprises"
```

## Updating

```sh
cd ~/portfolio-digest
git pull
docker compose up -d db backup
docker compose run --rm --build migrate
```

## Backups

The `backup` service dumps the database when it starts and every 24 hours after that, into
`BACKUP_DIR` (default `./backups`). Files older than `BACKUP_KEEP_DAYS` (default 14) are
deleted. Copy that folder into your normal off-server backups.

Restore into the running database:

```sh
docker compose exec -T db pg_restore -U postgres --clean --if-exists -d portfolio \
  < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
```

## Notes

- The database passwords only take effect when the `pgdata` volume is first created.
  Changing them later in `.env` needs an `ALTER ROLE` inside the database as well.
- The app logs in as the `portfolio` role, which owns its own database but is not a
  superuser. The `postgres` superuser is used only by the database and backup containers.
