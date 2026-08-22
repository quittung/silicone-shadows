#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP_DIR=/opt/silicone-shadows
PUBLIC_URL=https://shadows.qtng.dev
RUNTIME_PATHS=(server static certificates outline.py catalog_source.json)

usage() {
  echo "Usage: admin/deploy.sh [--apply] [--allow-dirty]"
  echo "Without --apply, show the runtime files that differ from production."
  echo "Deploying an unclean worktree requires --allow-dirty."
}

apply=0
allow_dirty=0
for argument in "$@"; do
  case "$argument" in
    --apply) apply=1 ;;
    --allow-dirty) allow_dirty=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

for command in git install mktemp rsync sha256sum ssh; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
[[ -f "$ROOT/.env" ]] || { echo "Missing $ROOT/.env" >&2; exit 1; }

read_setting() {
  sed -n "s/^$1=//p" "$ROOT/.env" | tail -n 1 | tr -d "'\""
}

server=$(read_setting SILICONE_SHADOWS_SERVER)
user=$(read_setting SILICONE_SHADOWS_USER)
[[ $server =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo "Invalid SILICONE_SHADOWS_SERVER" >&2; exit 1; }
[[ $user =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "Invalid SILICONE_SHADOWS_USER" >&2; exit 1; }
target=$user@$server
ssh_options=(-o BatchMode=yes -o ConnectTimeout=8)

deploy_tmp=$(mktemp -d)
cleanup() {
  [[ -n ${deploy_tmp:-} && -d $deploy_tmp ]] && rm -rf -- "$deploy_tmp"
}
trap cleanup EXIT
snapshot=$deploy_tmp/runtime
mkdir -p "$snapshot/server" "$snapshot/static" "$snapshot/certificates"

while IFS= read -r -d '' relative; do
  [[ -f "$ROOT/$relative" ]] || continue
  install -D -m 0644 "$ROOT/$relative" "$snapshot/$relative"
done < <(git -C "$ROOT" ls-files -z --cached --others --exclude-standard -- "${RUNTIME_PATHS[@]}")

commit=$(git -C "$ROOT" rev-parse HEAD)
dirty=0
[[ -z $(git -C "$ROOT" status --porcelain) ]] || dirty=1
stamp=$(date -u +%Y%m%dT%H%M%SZ)
printf 'commit=%s\ndirty=%s\ndeployed_at=%s\n' "$commit" "$dirty" "$stamp" > "$snapshot/DEPLOYED_REVISION"
(
  cd "$snapshot"
  find . -type f ! -name .deploy-manifest -print0 | sort -z | xargs -0 sha256sum > .deploy-manifest
)

local_requirements=$(sha256sum "$ROOT/requirements.txt" | cut -d ' ' -f 1)
remote_requirements=$(ssh "${ssh_options[@]}" "$target" "sha256sum '$APP_DIR/requirements.txt'" | cut -d ' ' -f 1)
if [[ $local_requirements != "$remote_requirements" ]]; then
  echo "requirements.txt differs from production; update the remote virtual environment and requirements file manually first" >&2
  exit 1
fi

preview_directory() {
  local directory=$1
  rsync --recursive --links --checksum --delete --dry-run --itemize-changes \
    --exclude='__pycache__/' \
    --no-perms --no-owner --no-group --omit-dir-times \
    "$snapshot/$directory/" "$target:$APP_DIR/$directory/"
}

backend_changes=$(
  preview_directory server
  preview_directory certificates
  for file in outline.py catalog_source.json; do
    rsync --checksum --dry-run --itemize-changes --no-perms --no-owner --no-group \
      "$snapshot/$file" "$target:$APP_DIR/$file"
  done
)
static_changes=$(preview_directory static)

if [[ -z $backend_changes && -z $static_changes ]]; then
  echo "Production runtime files already match this worktree."
  exit 0
fi

echo "Runtime changes:"
[[ -z $backend_changes ]] || printf '%s\n' "$backend_changes"
[[ -z $static_changes ]] || printf '%s\n' "$static_changes"
if [[ $apply -eq 0 ]]; then
  echo "Dry run only. Re-run with --apply to deploy."
  exit 0
fi
if [[ $dirty -eq 1 && $allow_dirty -eq 0 ]]; then
  echo "Refusing to deploy an unclean worktree; re-run with --apply --allow-dirty." >&2
  exit 1
fi

command -v node >/dev/null || { echo "Missing command: node" >&2; exit 1; }
git -C "$ROOT" diff --check
"$ROOT/.venv/bin/python" -m unittest discover -s "$ROOT/tests" -q
while IFS= read -r -d '' script; do
  node --check "$script"
done < <(find "$ROOT/static" -maxdepth 1 -type f -name '*.js' -print0 | sort -z)

remote_stage=$APP_DIR/.deploy-staging/$stamp
ssh "${ssh_options[@]}" "$target" "mkdir -p '$remote_stage'"
rsync --archive --delete "$snapshot/" "$target:$remote_stage/"
backend_changed=0
[[ -z $backend_changes ]] || backend_changed=1

ssh "${ssh_options[@]}" "$target" bash -s -- "$stamp" "$backend_changed" "$PUBLIC_URL" <<'REMOTE'
set -euo pipefail

stamp=$1
backend_changed=$2
public_url=$3
app=/opt/silicone-shadows
service=silicone-shadows
stage=$app/.deploy-staging/$stamp
backup=$app/.deploy-backups/$stamp
backup_runtime=$backup/runtime

cd "$stage"
sha256sum --quiet -c .deploy-manifest
systemctl is-active --quiet "$service"
mkdir -p "$backup_runtime"
for directory in server static certificates; do
  rsync -a "$app/$directory/" "$backup_runtime/$directory/"
done
cp -a "$app/outline.py" "$app/catalog_source.json" "$backup_runtime/"
for marker in DEPLOYED_COMMIT DEPLOYED_REVISION DEPLOYED_MANIFEST.sha256; do
  [[ ! -e $app/$marker ]] || cp -a "$app/$marker" "$backup_runtime/"
done

if [[ $backend_changed -eq 1 ]]; then
  "$app/.venv/bin/python" - "$backup/state.sqlite3" <<'PY'
import sqlite3
import sys

with sqlite3.connect("/var/lib/silicone-shadows/state.sqlite3") as source:
    with sqlite3.connect(sys.argv[1]) as target:
        source.backup(target)
PY
  chmod 0600 "$backup/state.sqlite3"
fi

sync_runtime() {
  local source=$1
  for directory in server certificates; do
    rsync -a --delete --exclude='__pycache__/' "$source/$directory/" "$app/$directory/"
  done
  install -m 0644 "$source/outline.py" "$source/catalog_source.json" "$app/"
  rsync -a --exclude='*.html' "$source/static/" "$app/static/"
  rsync -a --delete "$source/static/" "$app/static/"
}

promoted=0
rollback() {
  local status=$?
  trap - ERR INT TERM HUP
  if [[ $promoted -eq 1 ]]; then
    sync_runtime "$backup_runtime"
    rm -f "$app/DEPLOYED_REVISION" "$app/DEPLOYED_MANIFEST.sha256"
    for marker in DEPLOYED_COMMIT DEPLOYED_REVISION DEPLOYED_MANIFEST.sha256; do
      [[ ! -e $backup_runtime/$marker ]] || cp -a "$backup_runtime/$marker" "$app/"
    done
    systemctl restart "$service"
  fi
  exit "$status"
}
trap rollback ERR INT TERM HUP

promoted=1
[[ $backend_changed -eq 0 ]] || systemctl stop "$service"
sync_runtime "$stage"
install -m 0644 "$stage/DEPLOYED_REVISION" "$app/DEPLOYED_REVISION"
install -m 0644 "$stage/.deploy-manifest" "$app/DEPLOYED_MANIFEST.sha256"
rm -f "$app/DEPLOYED_COMMIT"
[[ $backend_changed -eq 0 ]] || systemctl start "$service"

ready=0
for _ in {1..30}; do
  if systemctl is-active --quiet "$service" && \
     curl --fail --silent --show-error -H 'Host: shadows.qtng.dev' \
       http://127.0.0.1:8000/api/session >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready -eq 1 ]]
(cd "$app" && sha256sum --quiet -c DEPLOYED_MANIFEST.sha256)
curl --fail --silent --show-error "$public_url/" >/dev/null
curl --fail --silent --show-error "$public_url/compare" >/dev/null
[[ $(curl --silent --output /dev/null --write-out '%{http_code}' "$public_url/api/items") == 401 ]]

trap - ERR INT TERM HUP
echo "Deployment verified: $stamp"
REMOTE
