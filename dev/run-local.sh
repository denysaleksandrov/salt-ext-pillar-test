#!/usr/bin/env bash
# Compile pillar masterless with salt-call --local (no salt-master needed).
#   dev/run-local.sh baseline|custom <minion_id> [extra salt-call args]
# Needs: dev/pg.sh up, and for `custom` also `python dev/mock_api.py --token devtoken`.
set -euo pipefail

MODE=${1:?usage: $0 baseline|custom <minion_id>}
MINION=${2:?usage: $0 baseline|custom <minion_id>}
shift 2

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN="$ROOT/dev/.run/$MODE"
mkdir -p "$RUN/minion.d"

cat >"$RUN/minion" <<EOF
id: $MINION
file_client: local
root_dir: $RUN
pki_dir: $RUN/pki
cachedir: $RUN/cache
log_file: $RUN/minion.log
sock_dir: $RUN/sock
user: $(id -un)
pillar_roots: {base: [$RUN/empty-pillar]}
file_roots: {base: [$RUN/empty-states]}
pillar_dirs: [$ROOT/_pillar]
EOF
mkdir -p "$RUN/empty-pillar" "$RUN/empty-states"
printf 'devtoken' >"$RUN/api.token"
sed "s#__RUN_DIR__#$RUN#g" "$ROOT/dev/local/$MODE.conf" >"$RUN/minion.d/ext_pillar.conf"

export PG_PILLAR_PW=${PG_PILLAR_PW:-salt_pillar}
exec "$ROOT/.venv/bin/salt-call" --local --config-dir="$RUN" --out=json "$@" pillar.items
