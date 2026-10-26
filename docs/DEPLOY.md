# Deploying on your server

The app runs as its own Docker Compose project, `portfolio-digest`, with its own Postgres
container. It is fully separate from anything else on the server, including any existing
Postgres 18. No ports are published: nothing here is reachable from outside the server.

## First install

```sh
git clone https://github.com/thatskiff33/nz-portfolio-digest.git ~/portfolio-digest
cd ~/portfolio-digest
cp .env.example .env
chmod 600 .env
# Fill in both passwords with: openssl rand -hex 32
# Set BACKUP_UID and BACKUP_GID to the output of `id -u` and `id -g`.
nano .env

mkdir -p backups                                 # as your own user, before first start
docker compose up -d db backup
docker compose run --rm --build migrate          # creates the tables
docker compose run --rm migrate health           # should print "OK: ..."
docker compose run --rm migrate portfolio add "Family Trust"
```

If you set `BACKUP_DIR` to somewhere else, create that folder instead. The `backup`
service refuses to start until the folder exists, so a typo cannot quietly send backups
nowhere.

## Updating

Take a fresh backup first, so a migration that goes wrong costs nothing:

```sh
cd ~/portfolio-digest
git pull
docker compose run --rm backup --once            # prints "backup: wrote ..."
docker compose up -d db backup
docker compose run --rm --build migrate
docker compose run --rm migrate health
```

## Backups

The `backup` service dumps the database when it starts and every 24 hours after that, into
`BACKUP_DIR` (default `./backups`). The files belong to `BACKUP_UID:BACKUP_GID` and only
that user can read them. Copy the folder into your normal off-server backups.

Dumps older than `BACKUP_KEEP_DAYS` (default 14) are deleted, but only straight after a
new dump succeeds. If backups start failing, the old ones stay until it is fixed.

To check backups are working:

```sh
docker compose ps backup        # "healthy" means a dump was written in the last 26 hours
docker compose logs backup      # one line per dump, and the reason for any failure
```

## Restoring

Restore into the running database. `--single-transaction` means a restore that fails
part-way changes nothing.

```sh
docker compose stop backup
docker compose exec -T db pg_restore -U postgres --clean --if-exists --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose run --rm migrate health
docker compose start backup
```

To move to a new server, follow **First install** with the same passwords in `.env`, but
stop after `mkdir -p backups`. Then start only the database, restore as above, and carry on
from `migrate`:

```sh
docker compose up -d db
docker compose exec -T db pg_restore -U postgres --clean --if-exists --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose up -d backup
docker compose run --rm --build migrate
docker compose run --rm migrate health
```

## Notes

- The database passwords only take effect when the `pgdata` volume is first created.
  Changing them later in `.env` needs an `ALTER ROLE` inside the database as well.
- The app and the backups log in as the `portfolio` role, which owns its own database but
  is not a superuser and cannot connect to any other database. The `postgres` superuser
  password is only given to the database container.
