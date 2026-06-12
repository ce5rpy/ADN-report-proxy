# adn-report-proxy

Optional standalone TCP proxy for **legacy dashboard backends** (HBMonitor / FDMR Monitor forks, etc.) that ship their own `dashboard.py` / `monitor.py` and speak **report wire v1** only.

**Not** used with **adn-monitor 2.x + React** — that pair is already compatible with **adn-server 2.x** with no proxy.

## Problem

| Legacy stack | Upstream server |
|--------------|-----------------|
| Old dashboard + bundled monitor (v1 wire) | adn-dmr-server v1 | Works |
| Old dashboard + bundled monitor (v1 wire) | adn-server 2.x (JSON) | Broken |

This proxy listens where the legacy monitor expects the server, connects upstream to the real server, and translates **v2 → v1**.

## Quick start (install)

Runtime dependencies: **Twisted**, **PyYAML** (listed in `requirements.txt`).

```bash
cd /opt/adn-report-proxy
pip3 install -r requirements.txt
cp report-proxy.yaml.example report-proxy.yaml
```

Configure **adn-server**, `report-proxy.yaml`, and the legacy dashboard as described in the next section, then start the proxy:

```bash
python3 report-proxy.py -c report-proxy.yaml
```

## Configuration (legacy dashboard + adn-server 2.x)

Use this when you run an **old** dashboard stack (legacy ADN-Dashboard, FDMR Monitor fork with `dashboard.cfg` / `monitor.cfg`) against **adn-server 2.x**. You do **not** need the proxy with **adn-monitor 2.x + React**.

### The only rule that matters: who talks to whom

```text
  ┌─────────────┐     TCP v2 JSON      ┌────────────────┐     TCP v1 pickle     ┌──────────────────┐
  │ adn-server  │ ◄─────────────────── │ report-proxy   │ ◄──────────────────── │ legacy dashboard │
  │  LISTENS    │   proxy is CLIENT    │  LISTENS       │   dashboard is CLIENT │  (monitor.py)    │
  │  port 4321  │                      │  port 4322     │                       │                  │
  └─────────────┘                      └────────────────┘                       └──────────────────┘
```

| Component | Role | Default port | Config file | Setting |
|-----------|------|--------------|-------------|---------|
| **adn-server** | Listens for report clients | **4321** | `adn-server.yaml` | `REPORTS.REPORT_PORT` |
| **report-proxy** | Connects **to** the server | 4321 | `report-proxy.yaml` | `UPSTREAM.PORT` |
| **report-proxy** | Listens for the dashboard | **4322** | `report-proxy.yaml` | `LISTEN.PORT` |
| **Legacy dashboard** | Connects **to** the proxy | **4322** | `dashboard.cfg` | `SERVER_PORT` |

**Do not** point the dashboard at `4321` when using the proxy — that is the server’s wire v2 port and the old dashboard will not understand it.

**Do not** point `UPSTREAM` at `4322` — that is the proxy’s own listen port (you would create a loop).

### Step 1 — adn-server (`adn-server.yaml`)

Reporting must be enabled and the proxy’s IP must be allowed.

```yaml
REPORTS:
  REPORT: true
  REPORT_INTERVAL: 60
  REPORT_PORT: 4321
  # IP of the machine running report-proxy (127.0.0.1 if same host)
  REPORT_CLIENTS: "127.0.0.1"
```

- **`REPORT: true`** — without this, nothing listens on the report port.
- **`REPORT_PORT: 4321`** — leave as default unless you changed it everywhere else too.
- **`REPORT_CLIENTS`** — comma-separated allowlist. If the proxy runs on another host, put **that host’s IP**, not only `127.0.0.1`.

Start (or restart) the server:

```bash
python3 adn-server.py -c adn-server.yaml
```

Check: something is listening on `4321` (e.g. `ss -tlnp | grep 4321`).

### Step 2 — report-proxy (`report-proxy.yaml`)

Edit the file you copied in **Quick start**:

```yaml
UPSTREAM:
  HOST: "127.0.0.1"   # adn-server IP (same machine → 127.0.0.1)
  PORT: 4321           # adn-server REPORT_PORT — NOT 4322

LISTEN:
  HOST: ""            # empty = all interfaces (0.0.0.0)
  PORT: 4322           # legacy dashboard connects HERE

LOGGER:
  LOG_LEVEL: INFO
```

Start the proxy **after** the server:

```bash
python3 report-proxy.py -c report-proxy.yaml
```

You should see log lines like `upstream connected` and `listening for legacy monitors on ...:4322`.

### Step 3 — legacy dashboard (`dashboard.cfg`)

Typical legacy dashboard INI (`dashboard.cfg` or `monitor.cfg` — same idea):

```ini
[SERVER CONNECTION]
SERVER_IP = 127.0.0.1
SERVER_PORT = 4322
```

| Field | Value |
|-------|--------|
| `SERVER_IP` | IP of the machine running **report-proxy** (not adn-server if they differ) |
| `SERVER_PORT` | **`LISTEN.PORT` from the proxy** (4322 in the example above) |

Start the dashboard monitor process as you usually do (e.g. `python3 monitor.py` or your systemd unit). The **web UI** (Apache/nginx) does not replace this TCP connection — the backend monitor must reach the proxy.

### Step 4 — start order

1. **adn-server** (report port up)
2. **report-proxy** (upstream connected)
3. **legacy dashboard** monitor backend

If the dashboard starts before the proxy, restart the monitor after the proxy is up (or wait for its reconnect loop).

### Step 5 — verify

| Check | Expected |
|-------|----------|
| Proxy log | `upstream connected to adn-server` |
| Proxy log | `listening for legacy monitors on ...:4322` |
| Proxy log (dashboard connect) | `legacy monitor connected from ...` |
| Proxy log (optional) | `replayed N cached v1 frame(s) to new legacy client` |
| Dashboard UI | Linked systems / peers appear (may take up to `REPORT_INTERVAL` seconds for bridges) |

Quick TCP test from the dashboard host (optional):

```bash
# Should connect (proxy listening); not a full protocol test
nc -zv 127.0.0.1 4322
```

### Same machine vs two machines

**Everything on one host** (lab / small install):

| Setting | Value |
|---------|--------|
| `REPORTS.REPORT_CLIENTS` | `127.0.0.1` |
| `UPSTREAM.HOST` | `127.0.0.1` |
| `SERVER_IP` in dashboard | `127.0.0.1` |

**Server and dashboard on different hosts** (proxy can sit with either):

Example: server `10.0.0.1`, proxy on dashboard machine `10.0.0.5`:

| Setting | Value |
|---------|--------|
| On server `adn-server.yaml` | `REPORT_CLIENTS: "10.0.0.5"` |
| On proxy `UPSTREAM` | `HOST: "10.0.0.1"`, `PORT: 4321` |
| On proxy `LISTEN` | `HOST: ""`, `PORT: 4322` |
| On dashboard `dashboard.cfg` | `SERVER_IP = 10.0.0.5`, `SERVER_PORT = 4322` |

Open firewall **only** where needed: dashboard → proxy `4322`, proxy → server `4321`.

### Common mistakes

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Dashboard never connects | `SERVER_PORT = 4321` | Use proxy port (**4322**) |
| Proxy cannot reach server | `UPSTREAM.PORT = 4322` | Use server port (**4321**) |
| Proxy log: upstream connect failed | Server down or wrong `UPSTREAM.HOST` | Start server; fix IP |
| Connect then immediate drop / no data | `REPORT_CLIENTS` blocks proxy IP | Add proxy host IP to `REPORT_CLIENTS`, reload server |
| `REPORT: false` | Report disabled | Set `REPORTS.REPORT: true` |
| Empty bridge table forever | Normal at first connect on v2 | Wait for `REPORT_INTERVAL` or a voice event; proxy caches routing when server pushes it |
| Using adn-monitor 2.x React | Wrong stack | Point monitor at server **directly** on `4321` — **no** report-proxy |

### Direct legacy server (no proxy)

If upstream is still **adn-dmr-server** (v1 wire), you do **not** need this proxy: set `SERVER_PORT = 4321` and connect straight to the legacy server. The proxy can passthrough v1, but that path is only for odd migrations — prefer direct connection.

## Wire translation (v2 → v1)

| Upstream (v2) | Downstream (v1) |
|---------------|-----------------|
| `HELLO` (`report_protocol: 2`) | `HELLO` (`protocol: 1`) |
| `STATE_SND` / `dashboard_state` | `CONFIG_SND` (pickle) |
| `ROUTING_TABLE_SND` | `BRIDGE_SND` (pickle) |
| `TOPOLOGY_SND` | `CONFIG_SND` (pickle) |
| `VOICE_EVENT_SND` | `BRDG_EVENT` (CSV) |
| v1 frames (passthrough) | unchanged |

## Architecture (strict clean)

```
Infrastructure → Application → Domain
```

| Layer | Package | Responsibility |
|-------|---------|----------------|
| **Domain** | `domain/` | Opcodes, errors, `Result` — no Twisted, no I/O |
| **Application** | `application/` | Ports, `V2ToV1Mapper`, `ProxyUseCases` |
| **Infrastructure** | `infrastructure/` | YAML config, pickle wire codec, Twisted TCP |

Dependency rule: infrastructure imports application + domain; application imports domain only.

## Status

- v2 → v1 mapper (HELLO, STATE_SND, TOPOLOGY, ROUTING, VOICE, DELTA merge)
- Empty `BRIDGE_SND` on v2 HELLO
- **Authoritative cache**: legacy `CONFIG_REQ` / `BRIDGE_REQ` answered from local snapshot (no upstream hit per dashboard refresh)
- Upstream refresh on cache miss; optional stale background refresh (30 s TTL)
- v2 upstream: cache miss → `STATE_REQ`; v1 passthrough: `CONFIG_REQ` / `BRIDGE_REQ`
- `relay_table_key` routing JSON → legacy `BRIDGE_SND` pickle
- Snapshot replay on legacy (re)connect

## License

Copyright (C) 2026 Rodrigo Pérez, CE5RPY <ce5rpy@qmd.cl>

This program is free software; you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation; either version 3 of the License, or (at your option) any later version.

See [LICENSE](LICENSE) for the full text.
