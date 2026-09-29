# Salt external pillars: PostgreSQL and HTTP API

Custom ext_pillar modules (`_pillar/`), tested against Salt 3008.2, alongside a baseline that uses the stock Salt modules.

| | Stock `postgres` | `pg_pillar` | Stock `http_json` | `http_pillar` |
|---|---|---|---|---|
| In Salt 3008 core | yes | custom | **no** (3006/3007 only) | custom |
| Several DBs / endpoints | no (one global `postgres:` key) | named profiles | one per `ext_pillar` entry | one per `ext_pillar` entry |
| TLS options | none | any libpq option (`sslmode`, `sslrootcert` …) | none (global `verify_ssl` only) | `verify`, `ca_bundle`, client cert/key |
| Timeouts | none | `connect_timeout`, `statement_timeout_ms` | global `http_*_timeout` | per entry `timeout: [connect, read]` |
| Secrets | plaintext in master config | `password_file` / `password_env` / `.pgpass` | plaintext `username`/`password` | `bearer_token_file`, `password_file` |
| Lookup key | `%s` = minion_id | `%(minion_id)s` | `%s` = minion_id, or grains (spoofable) | `{minion_id}` only |
| HTTP method | n/a | n/a | GET | any, plus `json_body` |
| On error | logs and returns partial/`{}` (fail open) | raises, shows in `_errors` (fail closed) | logs and returns `{}` (fail open) | raises (fail closed); 404 → `{}` |
| Connections | new connection every render | reused per master worker, read-only session | new request every render | new request every render |

**Fail closed** means an exception from the module is caught by Salt and added to the pillar's `_errors` key. That makes `state.apply` refuse to run instead of applying states with missing pillar data. Set `fail_closed: False` on an entry to get the stock behaviour.

## Data model (PostgreSQL)

`sql/schema.sql` creates a `pillar_data(target, priority, data jsonb)` table. Rows with `target = 'default'` apply to every minion, and rows with `target = <minion_id>` apply to that minion. The configured query returns defaults first and then per-minion rows by priority, and they are deep-merged in that order. Any query works as long as it returns one JSON object column.

## Pillar API server (`api/pillar_api.py`)

A small HTTP server that `http_pillar` can query. It uses only the standard library, plus psycopg2 for the Postgres backend and PyYAML for YAML files. It serves pillar data from one of two backends:

```bash
# From a YAML/JSON dict: {default: {...}, <minion_id>: {...}}. The file is re-read when it changes.
python api/pillar_api.py dict api/example_pillars.yaml

# From the pillar_data table. Any libpq conninfo or postgresql:// URI works; the password comes from PGPASSWORD or ~/.pgpass.
python api/pillar_api.py postgres "host=pg.example.com dbname=salt user=salt_pillar sslmode=verify-full"
```

Common options are `--host`, `--port` (default 8088), `--token-file` (or the `PILLAR_API_TOKEN` env var, which makes the server require `Authorization: Bearer <token>`), and `--tls-cert` with `--tls-key`. The Postgres backend also takes `--query` and `--pool-size`.

| Endpoint | Response |
|---|---|
| `GET /pillar/<minion_id>` | The `default` entry deep-merged with the minion's own entry. 404 if neither exists. |
| `POST /lookup` `{"id": "<minion_id>"}` | Same data, for `method: POST` with `json_body: {id: "{minion_id}"}`. |
| `GET /healthz` | 200, or 503 when the backend is down. No auth, so it can be used for load balancer checks. |

Merging: nested dicts are merged recursively, and lists and scalar values from the minion's entry replace the default. Because of the `default` entry, every minion gets at least the defaults. If a backend error occurs, the server returns 503, and `http_pillar` fails closed.

Matching `ext_pillar` entry:

```yaml
ext_pillar:
  - http_pillar:
      url: https://pillar-api.example.com:8088/pillar/{minion_id}
      bearer_token_file: /etc/salt/secrets/pillar-api.token
      ca_bundle: /etc/salt/pki/pillar-api-ca.pem
```

## Development setup

Create the venv from pyenv's Python 3.13.2 (not the system python):

```bash
$(pyenv root)/versions/3.13.2/bin/python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` pins the local dev and test tools. The salt-master itself only needs psycopg2 (see [Deploying to a master](#deploying-to-a-master)).

Format code with Black:

```bash
.venv/bin/black --exclude '/(\.venv|\.git|dev/\.run)/' .
```

## Testing

The tests replace the database and HTTP calls with fakes, and run the API server in-process on a random port. They don't need a real database, API or salt-master.

```bash
.venv/bin/pytest -q tests                                     # all tests
.venv/bin/pytest -q tests/test_pg_pillar.py::test_rows_merged_in_order   # a single test
```

Coverage (`pytest-cov` is in `requirements.txt`):

```bash
.venv/bin/pytest -q --cov=_pillar --cov=api --cov-report=term-missing tests
.venv/bin/pytest -q --cov=_pillar --cov=api --cov-report=html tests    # then open htmlcov/index.html
```

## Deploying to a master

1. Install the DB driver into the master's Python: `salt-pip install psycopg2-binary`. `requests` is already bundled.
2. Copy `_pillar/*.py` to `<file_roots>/_pillar/` and run `salt-run saltutil.sync_pillar`, or copy them to `<extension_modules>/pillar/`.
3. Adapt `config/master.d/20-custom.conf` (or `10-baseline.conf`) into `/etc/salt/master.d/`, then restart `salt-master`.
4. Check the result: run `salt <minion> saltutil.refresh_pillar`, then `salt <minion> pillar.items`, and look for `_errors`.

Unknown ext_pillar names are only logged as critical and skipped. If a module isn't synced, its data silently goes missing, so check the master log after deploying.
