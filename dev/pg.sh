#!/usr/bin/env bash
# Local PostgreSQL for ext_pillar testing (podman).
#   dev/pg.sh up     start container and load sql/schema.sql + sql/seed.sql
#   dev/pg.sh seed   reload sql/seed.sql
#   dev/pg.sh psql   open psql as the superuser
#   dev/pg.sh down   remove the container
#
# Environment:
#   PG_PORT=55432          port Postgres is reachable on
#   PG_BIND=127.0.0.1      address to listen on; 0.0.0.0 to allow a remote salt-master
#   PG_NETWORK=bridge      bridge: podman port mapping (-p)
#                          host:   share the host network, Postgres listens on PG_PORT
#                                  directly (use when podman port forwarding is blocked)
#   PG_IMAGE=docker.io/library/postgres:16
#
# Example for a remote master: PG_NETWORK=host PG_BIND=0.0.0.0 dev/pg.sh up
set -euo pipefail

NAME=ext-pillar-pg
PORT=${PG_PORT:-55432}
BIND=${PG_BIND:-127.0.0.1}
NETWORK=${PG_NETWORK:-bridge}
IMAGE=${PG_IMAGE:-docker.io/library/postgres:16}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# podman exec inherits the container env, so PGPORT (host mode) applies here too.
psql_in() {
    podman exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U postgres -d salt "$@"
}

case "${1:-}" in
up)
    case "$NETWORK" in
    bridge)
        net_args=(-p "${BIND}:${PORT}:5432")
        pg_args=()
        ;;
    host)
        # PGPORT is honoured by postgres, the image entrypoint, pg_isready and psql.
        net_args=(--network host -e "PGPORT=${PORT}")
        pg_args=(-c "listen_addresses=${BIND}")
        ;;
    *) echo "PG_NETWORK must be bridge or host" >&2; exit 2 ;;
    esac
    podman run -d --name "$NAME" "${net_args[@]}" \
        -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=salt "$IMAGE" ${pg_args[@]+"${pg_args[@]}"} >/dev/null
    until podman exec "$NAME" pg_isready -U postgres -d salt -q; do sleep 1; done
    # The entrypoint restarts the server once after init; wait for the final one.
    sleep 2
    until podman exec "$NAME" pg_isready -U postgres -d salt -q; do sleep 1; done
    psql_in <"$ROOT/sql/schema.sql"
    psql_in <"$ROOT/sql/seed.sql"
    echo "postgres ready on ${BIND}:${PORT} via ${NETWORK} network" \
        "(db=salt user=salt_pillar pw=salt_pillar)"
    ;;
seed) psql_in <"$ROOT/sql/seed.sql" ;;
psql) podman exec -it "$NAME" psql -U postgres -d salt ;;
down) podman rm -f "$NAME" >/dev/null && echo "removed $NAME" ;;
*) echo "usage: $0 up|seed|psql|down" >&2; exit 2 ;;
esac
