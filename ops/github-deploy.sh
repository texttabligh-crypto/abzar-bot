#!/usr/bin/env bash
# Install as /usr/local/sbin/abzar-github-deploy, owned by root, mode 700.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
case "${SSH_ORIGINAL_COMMAND:-}" in
  check) echo 'ABZAR DEPLOY CONNECTION OK'; exit 0 ;;
  deploy) ;;
  *) echo 'Only check or deploy is permitted.' >&2; exit 1 ;;
esac
exec 9>/run/lock/abzar-deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
cd /opt/trading-bot
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
  echo 'Tracked local changes found; refusing to overwrite.' >&2; exit 1;
}
[[ "$(git branch --show-current)" == main ]] || exit 1
git fetch origin main
# Refuse divergent history before changing anything.
git merge-base --is-ancestor HEAD origin/main
install -d -m 700 /root/trading-bot-backups
umask 077
backup="/root/trading-bot-backups/database-$(date +%Y%m%d-%H%M%S).sql"
if ! docker exec tradingbot_postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$backup" || [[ ! -s "$backup" ]]; then
  echo 'Backup failed. Deployment stopped.' >&2
  exit 1
fi
echo "Backup saved: $backup"
git merge --ff-only origin/main
docker compose build bot
docker compose up -d --no-deps bot
# Require the process to stay alive, not just a successful container creation.
sleep 20
[[ "$(docker inspect -f '{{.State.Running}}' tradingbot_app)" == true ]]
[[ "$(docker inspect -f '{{.RestartCount}}' tradingbot_app)" == 0 ]]
docker exec tradingbot_app python -c 'from app.version import APP_VERSION; print("Installed version:", APP_VERSION)'
echo "DEPLOY OK: $(git rev-parse HEAD)"
