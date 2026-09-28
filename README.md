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

## Deploying to a master

1. Install the DB driver into the master's Python: `salt-pip install psycopg2-binary`. `requests` is already bundled.
2. Copy `_pillar/*.py` to `<file_roots>/_pillar/` and run `salt-run saltutil.sync_pillar`, or copy them to `<extension_modules>/pillar/`.
3. Adapt `config/master.d/20-custom.conf` (or `10-baseline.conf`) into `/etc/salt/master.d/`, then restart `salt-master`.
4. Check the result: run `salt <minion> saltutil.refresh_pillar`, then `salt <minion> pillar.items`, and look for `_errors`.

Unknown ext_pillar names are only logged as critical and skipped. If a module isn't synced, its data silently goes missing, so check the master log after deploying.
