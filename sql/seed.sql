TRUNCATE pillar_data;

INSERT INTO pillar_data (target, priority, data) VALUES
    ('default', 10, '{"ntp": {"servers": ["0.pool.ntp.org", "1.pool.ntp.org"]}, "env": "prod", "users": {"admin": {"shell": "/bin/bash"}}}'),
    ('default', 20, '{"motd": "Managed by Salt"}'),
    ('minion1', 10, '{"env": "dev", "users": {"alice": {"shell": "/bin/zsh"}}}'),
    ('minion1', 20, '{"ntp": {"servers": ["ntp.dev.example.com"]}}'),
    ('minion2', 10, '{"role": "db", "postgres": {"version": 16}}');
