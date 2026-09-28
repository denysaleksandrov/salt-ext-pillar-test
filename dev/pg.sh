#!/usr/bin/env bash
# Local PostgreSQL for ext_pillar testing (podman).
#   dev/pg.sh up     start container and load sql/schema.sql + sql/seed.sql
#   dev/pg.sh seed   reload sql/seed.sql
#   dev/pg.sh psql   open psql as the superuser
#   dev/pg.sh down   remove the container
set -euo pipefail

NAME=ext-pillar-pg
PORT=${PG_PORT:-55432}
IMAGE=${PG_IMAGE:-docker.io/library/postgres:16}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

psql_in() {
    podman exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U postgres -d salt "$@"
}

case "${1:-}" in
up)
    podman run -d --name "$NAME" -p "127.0.0.1:${PORT}:5432" \
        -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=salt "$IMAGE" >/dev/null
    until podman exec "$NAME" pg_isready -U postgres -d salt -q; do sleep 1; done
    # The entrypoint restarts the server once after init; wait for the final one.
    sleep 2
    until podman exec "$NAME" pg_isready -U postgres -d salt -q; do sleep 1; done
    psql_in <"$ROOT/sql/schema.sql"
    psql_in <"$ROOT/sql/seed.sql"
    echo "postgres ready on 127.0.0.1:${PORT} (db=salt user=salt_pillar pw=salt_pillar)"
    ;;
seed) psql_in <"$ROOT/sql/seed.sql" ;;
psql) podman exec -it "$NAME" psql -U postgres -d salt ;;
down) podman rm -f "$NAME" >/dev/null && echo "removed $NAME" ;;
*) echo "usage: $0 up|seed|psql|down" >&2; exit 2 ;;
esac
