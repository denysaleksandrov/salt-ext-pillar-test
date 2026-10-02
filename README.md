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

Masterless end-to-end check on a workstation: start Postgres with `dev/pg.sh up` and the API with `PILLAR_API_TOKEN=devtoken .venv/bin/python api/pillar_api.py dict api/example_pillars.yaml`, then run `dev/run-local.sh custom minion1`. The script builds a throwaway minion config under `dev/.run/` and runs `salt-call --local pillar.items`, so no master is needed.

## Deploying to a master

The setup has up to three parts. Only the Salt master needs Salt. Postgres and the API can run on any host the master can reach.

```
salt-master ──pg_pillar──▶ PostgreSQL (pillar_data table)
     │
     └──http_pillar──▶ pillar API (api/pillar_api.py) ──▶ YAML file or the same PostgreSQL
```

Pillar is built **on the master**, so connections, firewall rules and secrets all live on the master side. Minions never talk to Postgres or the API.

### Step 1: Database

**Option A: lab database in podman (RHEL).** Copy the repo (at least `dev/` and `sql/`) to the DB host, then run:

```bash
PG_NETWORK=host PG_BIND=0.0.0.0 dev/pg.sh up      # Postgres 16 on 0.0.0.0:55432, loads sql/schema.sql and sql/seed.sql
ss -ltnp | grep 55432                             # must show "postgres"
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<master-ip>/32" port port="55432" protocol="tcp" accept'
firewall-cmd --reload
```

- **Use `PG_NETWORK=host`.** The default bridge mode publishes the port through podman's forwarding, which on RHEL 10 can silently drop traffic from other hosts. The symptom is the master's SYNs arriving in `tcpdump` and never getting an answer, with `ss` showing `conmon` instead of `postgres`.
- **SSL is off** in this container, so the master profile needs `sslmode: disable`.
- **The login is fixed:** `up` creates the read-only role `salt_pillar` with the password `salt_pillar`.

**Option B: existing PostgreSQL server.**

1. Change the `salt_pillar` password in `sql/schema.sql`.
2. Load it: `psql "host=<pg-host> dbname=salt user=<admin>" -v ON_ERROR_STOP=1 -f sql/schema.sql`.
3. Allow the master's IP in `pg_hba.conf` for `salt_pillar` (`hostssl salt salt_pillar <master-ip>/32 scram-sha-256`), and reload Postgres.

`sql/seed.sql` is sample data and starts with `TRUNCATE pillar_data`, so don't load it into a database that has real data. Re-running `schema.sql` doesn't change an existing role's password. Use `ALTER ROLE salt_pillar PASSWORD '...'` for that.

**Add data for your minions.** Each row is one JSON object. `default` rows apply to every minion, and the minion's own rows are merged on top in `priority` order:

```sql
INSERT INTO pillar_data (target, priority, data) VALUES
  ('deployer', 10, '{"role": "deployer", "ntp": {"servers": ["ntp1.example.com"]}}');

-- Preview what a minion will get, in merge order:
SELECT target, priority, data FROM pillar_data
 WHERE target IN ('default', 'deployer') ORDER BY (target <> 'default'), priority;
```

### Step 2: Pillar API (only if you use `http_pillar`)

The server needs Python 3.9 or newer, without Salt: `pip install psycopg2-binary PyYAML`. Install only the library for the backend you use.

**TLS certificate.** Create a small CA and a server certificate. Put every hostname and IP the master will use in the URL into `subjectAltName`. The extra fields are required by recent Python versions in strict mode.

```bash
cat > ca.ext <<'EOF'
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
cat > server.ext <<'EOF'
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:pillar-api.example.com, IP:172.21.0.210
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
openssl req -new -newkey rsa:4096 -nodes -keyout ca.key -out ca.csr -subj "/CN=Pillar API CA"
openssl x509 -req -in ca.csr -signkey ca.key -out ca.pem -days 3650 -sha256 -extfile ca.ext
openssl req -new -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=pillar-api.example.com"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem -days 825 -sha256 -extfile server.ext
openssl verify -CAfile ca.pem server.pem      # server.pem: OK
```

| File | Goes to |
|---|---|
| `server.pem`, `server.key` | API host, e.g. `/etc/pillar-api/` (key `0600`, owned by the service user) |
| `ca.pem` | Salt master, `/etc/salt/pki/pillar-api-ca.pem` (`0644`) |
| `ca.key` | Offline. Needed only to issue or renew certificates. |

If you use a corporate CA instead, send it `server.csr`, put any intermediate certificates after the server certificate in `server.pem`, and give the master that CA's root certificate.

**Token and service.** Create the token with `openssl rand -hex 32 > /etc/pillar-api/token`, and give the master the same value (Step 3). An example systemd unit, `/etc/systemd/system/pillar-api.service`:

```ini
[Unit]
Description=Salt pillar API
Wants=network-online.target
After=network-online.target

[Service]
User=pillar-api
# Postgres backend only: password via a pgpass file (host:port:db:user:password)
# Environment=PGPASSFILE=/etc/pillar-api/pgpass
ExecStart=/opt/pillar-api/venv/bin/python /opt/pillar-api/pillar_api.py \
  dict /etc/pillar-api/pillars.yaml \
  --host 0.0.0.0 --port 8088 --token-file /etc/pillar-api/token \
  --tls-cert /etc/pillar-api/server.pem --tls-key /etc/pillar-api/server.key
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

For the Postgres backend, replace `dict /etc/pillar-api/pillars.yaml` with `postgres "host=<pg-host> port=5432 dbname=salt user=salt_pillar sslmode=verify-full"`. Then enable it and check it:

```bash
systemctl daemon-reload && systemctl enable --now pillar-api
firewall-cmd --permanent --add-port=8088/tcp && firewall-cmd --reload
curl -s --cacert ca.pem https://pillar-api.example.com:8088/healthz      # {"status": "ok"}
curl -s --cacert ca.pem -H "Authorization: Bearer $(cat /etc/pillar-api/token)" \
  https://pillar-api.example.com:8088/pillar/minion1
```

The server reads its certificate only at startup, so restart it after renewing the certificate. Changes to the YAML file are picked up without a restart.

### Step 3: Salt master

Run these as root on the master. From 3006 on, `salt-master` runs as the **`salt`** user, so every file it reads must be readable by that user.

**3.1 Postgres driver** (skip if you only use `http_pillar`; `requests` is already bundled):

```bash
salt-pip install psycopg2-binary
sudo -u salt /opt/saltstack/salt/bin/python3 -c "import psycopg2; print(psycopg2.__version__)"
```

**3.2 Modules.** Use the file server and sync, or copy the files straight into the extension modules directory:

```bash
# a) via the file server (your _pillar dir must be under the base file_roots; check: salt-run config.get file_roots)
cp _pillar/pg_pillar.py _pillar/http_pillar.py /srv/salt/_pillar/
salt-run saltutil.sync_pillar                      # must list pillar.pg_pillar / pillar.http_pillar

# b) directly
EXT=$(salt-run config.get extension_modules --out=newline_values_only)
mkdir -p $EXT/pillar && cp _pillar/pg_pillar.py _pillar/http_pillar.py $EXT/pillar/
```

**3.3 Secrets and CA:**

```bash
install -d -m 0750 -o root -g salt /etc/salt/secrets
printf '%s' '<salt_pillar password>' > /etc/salt/secrets/pg_pillar.pw
printf '%s' '<API token>'            > /etc/salt/secrets/pillar-api.token
chown root:salt /etc/salt/secrets/*; chmod 0640 /etc/salt/secrets/*
install -m 0644 ca.pem /etc/salt/pki/pillar-api-ca.pem
```

**3.4 Config.** Use `config/master.d/20-custom.conf` as the template and save it as `/etc/salt/master.d/50-ext-pillar.conf`. For the podman lab DB from Option A:

```yaml
pg_pillar:
  profiles:
    main:
      host: 172.21.0.210
      port: 55432
      dbname: salt
      user: salt_pillar
      password_file: /etc/salt/secrets/pg_pillar.pw
      sslmode: disable                 # production: verify-full + sslrootcert
      connect_timeout: 5
      statement_timeout_ms: 5000

ext_pillar:
  - pg_pillar:
      profile: main
      queries:
        - >-
          SELECT data FROM pillar_data
           WHERE target IN ('default', %(minion_id)s)
           ORDER BY (target <> 'default'), priority
  - http_pillar:
      url: https://pillar-api.example.com:8088/pillar/{minion_id}
      namespace: api                   # optional: data lands under pillar['api']
      bearer_token_file: /etc/salt/secrets/pillar-api.token
      ca_bundle: /etc/salt/pki/pillar-api-ca.pem
      timeout: [5, 15]
```

Salt merges `ext_pillar` lists from all `master.d/*.conf` files in alphabetical order of file name. Existing entries, such as VCF's `sseapi`, are kept. Don't repeat an entry in another file, or it runs twice.

**3.5 Restart and check the loaded config:**

```bash
systemctl restart salt-master
salt-run config.get ext_pillar        # your entries are listed, in order
salt-run config.get pg_pillar         # the profile is there
```

### Step 4: Test

**4.1 Network from the master.** Each command should print `0`:

```bash
timeout 5 bash -c '</dev/tcp/172.21.0.210/55432'; echo $?            # Postgres port
sudo -u salt /opt/saltstack/salt/bin/python3 -c "import psycopg2; psycopg2.connect(host='172.21.0.210', port=55432, dbname='salt', user='salt_pillar', password=open('/etc/salt/secrets/pg_pillar.pw').read().strip(), sslmode='disable', connect_timeout=5); print('ok')"
curl -s --cacert /etc/salt/pki/pillar-api-ca.pem -H "Authorization: Bearer $(cat /etc/salt/secrets/pillar-api.token)" \
  https://pillar-api.example.com:8088/pillar/minion1
```

**4.2 Build a pillar on the master.** No minion is needed, and this is the quickest check:

```bash
salt-run pillar.show_pillar minion1
salt-run pillar.show_pillar minion1 -l debug 2>&1 | grep -iE 'pg_pillar|http_pillar|ext_pillar|psycopg'
```

The output should contain the merged DB data, plus the API data under `api`, and no `_errors` key.

**4.3 Check it from a minion:**

```bash
salt minion1 saltutil.refresh_pillar
salt minion1 pillar.items
salt minion1 pillar.get _errors       # empty
```

`pillar.get` and `pillar.raw` only return what the minion already has in memory. They never trigger a new pillar build, so run `refresh_pillar` first.

**4.4 Change the data and see it update.** Change a row, or the API's YAML file. Then run `salt minion1 saltutil.refresh_pillar` followed by `salt minion1 pillar.items`. No master restart is needed. If `pillar_cache` is enabled, the change appears only after `pillar_cache_ttl`.

**4.5 Check that failures stop state runs.** Stop Postgres or the API, then:

```bash
salt-run pillar.show_pillar minion1   # _errors: Failed to load ext_pillar pg_pillar: ...
salt minion1 state.apply test=True    # refuses to run: "Pillar failed to render"
```

Start it again and repeat 4.3.

**4.6 Check from the Postgres side.** Each master worker keeps an open connection:

```sql
SELECT pid, usename, client_addr, state, backend_start
  FROM pg_stat_activity WHERE application_name = 'salt-pg-pillar';
```

### When to restart what

| Change | Action |
|---|---|
| `master.d/*.conf` (ext_pillar list, profiles, `pillar_cache` …) | `systemctl restart salt-master` |
| `salt-pip install psycopg2-binary` | restart salt-master |
| Module code in `_pillar/` | `salt-run saltutil.sync_pillar` (no restart; modules are reloaded for every pillar build) |
| Pillar data (DB rows, API YAML) | `saltutil.refresh_pillar` on the minions |
| Secret files (password, token) | Nothing. They're read on each new connection or request. |
| API server certificate | restart the API server |

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[CRITICAL] ext_pillar interface named pg_pillar is unavailable`, and the data is silently missing | The module isn't in `<extension_modules>/pillar/` (3.2), or psycopg2 isn't importable by the `salt` user (3.1) |
| `_errors: ... timeout expired` | Network or firewall. Check with `tcpdump -nni <iface> port <port>` on the DB host. SYNs arriving with no reply means a firewall, or podman bridge forwarding (use `PG_NETWORK=host`) |
| `_errors: ... password authentication failed for user "salt_pillar"` | The password file doesn't match the DB, or the role is missing because `schema.sql` wasn't loaded. `podman logs ext-pillar-pg` shows which one |
| `_errors: ... server does not support SSL connections` | The profile says `sslmode: verify-full`/`require`, but SSL is off on the server (lab container) |
| `_errors: ... Permission denied: '/etc/salt/secrets/...'` | The `salt` user can't read the file: `chown root:salt`, `chmod 0640` |
| `_errors: ... 401` from `http_pillar` | The master's token file doesn't match the API's `--token-file` / `PILLAR_API_TOKEN` |
| `_errors: ... certificate verify failed` / hostname mismatch | Wrong `ca_bundle`, or the URL's hostname or IP isn't in the certificate's `subjectAltName` |
| A minion only gets the `default` data | There are no rows (or no API entry) for that exact minion ID. Check with `salt-call grains.get id` on the minion |
| Pillar data looks out of date | Run `saltutil.refresh_pillar`; with `pillar_cache`, wait for `pillar_cache_ttl` |
