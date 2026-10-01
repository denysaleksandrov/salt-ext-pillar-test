# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This repo holds custom Salt external pillar modules that read pillar data from PostgreSQL (`_pillar/pg_pillar.py`) and an HTTP JSON API (`_pillar/http_pillar.py`). The target is Salt 3008, where the stock `http_json` module is no longer in core. `README.md` has the stock-vs-custom comparison and the deployment steps.

## Environment

- Use the project venv `.venv`, built from pyenv Python 3.13.2 with `pip install -r requirements.txt`. Never use the system python.
- Format with Black: `.venv/bin/black --exclude '/(\.venv|\.git|dev/\.run)/' .`
- pip's index is the Broadcom Artifactory, which needs the VPN.
- Containers run under podman (no docker, and no compose provider installed).

## Commands

- Unit tests: `.venv/bin/pytest -q tests`
- Single test: `.venv/bin/pytest -q tests/test_pg_pillar.py::test_rows_merged_in_order`
- Local Postgres: `dev/pg.sh up|seed|psql|down` (podman, 127.0.0.1:55432, loads `sql/`). For a remote master use `PG_NETWORK=host PG_BIND=0.0.0.0 dev/pg.sh up`: on the user's RHEL 10 lab host, podman's bridge port forwarding drops external traffic.
- Pillar API server: `PILLAR_API_TOKEN=devtoken .venv/bin/python api/pillar_api.py dict api/example_pillars.yaml` or `... postgres "<conninfo>"` (127.0.0.1:8088)
- Masterless end-to-end: `dev/run-local.sh baseline|custom <minion_id>`. It generates a config under `dev/.run/` and runs `salt-call --local pillar.items`.
- The user plans to validate against a real PG database and a real Salt environment. Don't start the podman or end-to-end flows unless asked.

## Architecture notes

- Salt calls `ext_pillar(minion_id, pillar, **entry_kwargs)` on the master. The mapping under each `ext_pillar:` entry becomes the kwargs, so the function signature is the config schema. Return only new data and never mutate `pillar`.
- Exceptions raised from `ext_pillar` are caught in `salt/pillar/__init__.py` (`Pillar.ext_pillar`) and appended to the pillar's `_errors`. Both modules rely on this to fail closed (`fail_closed=True` by default).
- Salt's loader re-executes pillar modules for every pillar compile (it builds a new `LazyLoader` per `Pillar`), so module globals don't persist. `pg_pillar` keeps its connection cache in a `sys.modules["_salt_pg_pillar_connections"]` holder, keyed by profile and reset after fork. Don't move it back to a module-level dict.
- Neither module does grain-based lookups, on purpose, because minions can spoof their grains. The only key is `minion_id`: a bind parameter in SQL and url-quoted in HTTP.
- `pg_pillar` connection settings live in master opts under `pg_pillar.profiles.<name>`. Anything not in `_OWN_OPTIONS` goes straight to `psycopg2.connect`. Rows are merged with `salt.utils.dictupdate.merge`, using the master's `pillar_source_merging_strategy` and `pillar_merge_lists`.
- `api/pillar_api.py` is a standalone server and must not import salt, because it runs off the master. Its URL layout and 404/503 semantics are the contract `http_pillar` relies on: 404 → `{}` (with `not_found_ok`), anything else → error. The dict backend and the Postgres backend (same `pillar_data` table and `default` target as `pg_pillar`) share `deep_merge`. `tests/test_pillar_api.py` runs it in a thread on port 0, including an end-to-end check with `http_pillar`.
- Tests load modules with `tests/conftest.py:load_module`, which runs them outside the loader and injects `__opts__`. Mock `psycopg2.connect` and `requests.request`.
- Config: `config/master.d/` holds production-shaped master snippets, and `dev/local/` holds matching local variants for `run-local.sh`. `pillar_dirs` in opts is how the local run finds `_pillar/`.
