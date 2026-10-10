# Operations

Running, updating, backing up and troubleshooting the converter.

## Ways to run it

| Setup | Who can use it | Command |
|---|---|---|
| One computer, no Docker | the person at that computer (and the local network, if it allows) | `./run_local.sh` (Mac/Linux) or `run_local.bat` (Windows) |
| One computer, Docker | same | `docker compose up -d --build` |
| Server, Docker + HTTPS | the whole team, from anywhere they're allowed | see [Server](#server-recommended-for-the-team) |

All three serve the app on port **8501** (http://localhost:8501 on the machine itself). Stop with `Ctrl + C`
(`run_local`) or `docker compose down` (Docker).

### One computer

1. Install Python 3.11+ (on a Mac: `brew install python@3.12`).
2. Run `./run_local.sh` from the project folder. The first run creates `.venv`, installs the libraries and copies
   `.env.example` to `.env`. Then edit `.env`: admin username and password, optional `ANTHROPIC_API_KEY`.
3. Open http://localhost:8501 and sign in with the admin account.

**Other people on the same network:**
- They can use `http://<this computer's IP>:8501`, or `http://<computer-name>.local:8501` on a Mac.
- This works only while the computer is awake, the app is running, and the network allows devices to reach each
  other. Guest and many office Wi-Fi networks don't.
- Never forward port 8501 from the router to the internet: there is no HTTPS.

### Docker on one computer

1. Install Docker Desktop (or Colima) and turn on "Start when you sign in".
2. In the project folder: `docker compose up -d --build`.
3. `restart: unless-stopped` starts the app again whenever Docker starts.

The `data/` and `schemas/` folders are mounted from the project folder, so data survives rebuilds. Keep the
computer from sleeping if others depend on it.

### Server (recommended for the team)

On a small Linux VM (1 vCPU, 2 GB RAM is enough) with a DNS name pointing at it:

```bash
curl -fsSL https://get.docker.com | sh                      # Docker
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
apt install -y caddy                                        # HTTPS in front of the app
cd /opt && git clone https://github.com/<owner>/file-converter.git && cd file-converter
cp .env.example .env && nano .env                           # admin account, API key (optional)
mkdir -p data && chown 1000 data                            # the container runs as user 1000
docker compose up -d --build
```

**Port:** in `docker-compose.yml`, publish the port to the machine only (`"127.0.0.1:8501:8501"`) so it's
reachable only through Caddy.

**Caddy:** set `/etc/caddy/Caddyfile` to the block below and run `systemctl reload caddy`. Caddy gets and renews
the HTTPS certificate automatically.
```
convert.yourcompany.com {
    reverse_proxy localhost:8501
}
```

**Private alternative:** instead of a public DNS name, keep the server private and reach it over a VPN such as
Tailscale.

## Users

**First admin:** when the user table is empty, the account from `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` is
created at start-up. After the first sign-in, remove `ADMIN_PASSWORD` from `.env`.

**Day to day:** administrators manage everyone on the **Users** page. From the command line:

```bash
# Docker
docker compose exec app python scripts/manage_users.py add jane --admin
docker compose exec app python scripts/manage_users.py password jane
docker compose exec app python scripts/manage_users.py list
docker compose exec app python scripts/manage_users.py delete jane
# Without Docker
.venv/bin/python scripts/manage_users.py add jane
```

**Single user:** `AUTH_MODE=none` turns the login off. Only use it for one person on their own computer.

## Settings

All settings are in `.env`; each one is explained in `.env.example`. The ones you may change:

| Setting | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Enables the optional Claude buttons. Everything else works without it. |
| `AI_ENABLED` | `true` | `false` turns Claude off without removing the key. |
| `AI_AUTO_MAP` | `false` | `true` = Claude also refines the column matching on every upload (costs a call per upload). |
| `AI_MASK_PHONES` | `true` | Partly mask phone digits in the sample rows sent for Check for mistakes. |
| `MAPPING_MODEL` / `REPAIR_MODEL` | `claude-sonnet-5-5` / `claude-haiku-5-5` | Models for mapping/notes/checks and for value fixes. |
| `AI_SAMPLE_VALUES` | `15` | Sample values per column shown in the app and sent to Claude. |
| `AUTH_MODE` | `password` | `none` = no login (single user only). |
| `APP_TITLE` | Import File Converter | Browser tab title and login heading. |
| `APP_TIMEZONE` | `America/New_York` | Times shown in the app; default client time zone for new imports. |
| `DEFAULT_PAGE` | `new` | `imports` opens the Imports page first. |
| `IMPORT_WORKERS` | `2` | Imports converted at the same time; the rest wait as *queued*. |
| `FILE_RETENTION_DAYS` | `30` | Days to keep each import's files. `0` = forever. Records and counts are always kept. |

Restart the app after changing `.env`: `Ctrl + C` then `./run_local.sh`, or `docker compose up -d`. The target
format (`schemas/target_schema.yaml`) is reloaded automatically when the file changes.

## Data and backups

Everything lives in `data/`:

```
data/converter.db            users, templates, import history (SQLite, WAL mode: also -wal and -shm files)
data/imports/<id>/           source.<ext> (uploaded file), converted file, rejected.csv, summary.txt,
                             source.parquet + audit.parquet (for the side-by-side view)
```

**What to back up:** the whole `data/` folder. Choose one way:
- **While running:** `sqlite3 data/converter.db ".backup data/backup.db"`, then copy `data/imports/` and the
  backup file.
- **Stopped:** stop the app (`docker compose down`) and copy `data/`.
- **Automatic:** on a server, the provider's disk snapshots. On a Mac, Time Machine.

**Space:** files of finished imports older than `FILE_RETENTION_DAYS` are deleted automatically when the app
starts, and then at most once an hour when an import is started. The import record and its counts stay; its page says the files were deleted.

**Customer data:** uploaded files contain customers' phone numbers. Keep `data/` on an encrypted disk, restrict
who can sign in, and set a retention period you are comfortable with.

## Updating

Update the code with `git pull` (or pull an update bundle), then restart:

```bash
# one computer
cd ~/path/to/file-converter && git pull && ./run_local.sh
# Docker (one computer or server)
cd /opt/file-converter && git pull && docker compose up -d --build
```

Imports running during a restart are marked **interrupted**; open them and click **Re-run this import**. The
sidebar shows the running version. Compare it with [CHANGELOG.md](CHANGELOG.md).

## Monitoring and troubleshooting

| Symptom | Check / fix |
|---|---|
| App doesn't start | `docker compose logs -f` (Docker) or the terminal running `run_local.sh`. Usually a typo in `.env` (numbers must be numbers) or in `schemas/target_schema.yaml` (the error names the problem). |
| `permission denied` running `run_local.sh` | `chmod +x run_local.sh` once. |
| Port 8501 already in use | Another copy is running: stop it, or set `PORT=8502 ./run_local.sh`. |
| Others on the network can't open the Network URL | The network blocks device-to-device traffic (common on guest/office Wi-Fi), or the computer is asleep. Test from a phone on the same Wi-Fi. |
| Old behaviour after an update | The app wasn't restarted, or a saved template from before the update is applied. Restart, then check the template on the mapping page. |
| Import stuck at *queued* | All workers are busy. Wait, or raise `IMPORT_WORKERS`. |
| Import *interrupted* | The app restarted mid-import. Re-run it. |
| Claude buttons missing or disabled | `ANTHROPIC_API_KEY` not set, or `AI_ENABLED=false`. Restart after changing `.env`. |
| Container shows *unhealthy* | `docker compose logs`. A running-but-unhealthy container is not restarted automatically: `docker compose restart`. |

**Health check:** Streamlit answers `GET /_stcore/health` with `ok`; the Docker healthcheck uses it.
