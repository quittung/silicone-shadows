#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 1 ]; then
    echo "Usage: sudo $0 /absolute/path/to/silicone-shadows" >&2
    exit 2
fi

app_dir=$(realpath "$1")
test -x "$app_dir/.venv/bin/python"
test -d "$app_dir/dataset"

service_user=silicone-shadows
service_home=/var/lib/silicone-shadows
nologin=$(command -v nologin)

if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --home-dir "$service_home" --create-home \
        --shell "$nologin" "$service_user"
fi

install -d -m 0700 -o "$service_user" -g "$service_user" \
    "$service_home" \
    "$service_home/images" \
    "$service_home/work" \
    "$service_home/pending" \
    "$service_home/dataset" \
    "$app_dir/.local" \
    "$app_dir/.local/catalog"

if [ -z "$(find "$service_home/dataset" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    cp -a "$app_dir/dataset/." "$service_home/dataset/"
fi

chown -R "$service_user:$service_user" "$service_home" "$app_dir/.local"
find "$service_home" -type d -exec chmod 0700 {} +
find "$service_home" -type f -exec chmod 0600 {} +
find "$app_dir/.local" -type d -exec chmod 0700 {} +
find "$app_dir/.local" -type f -exec chmod 0600 {} +

echo "Service account and private hosted storage are ready."
echo "Copy deploy/silicone-shadows.service.example to /etc/systemd/system/"
echo "after replacing APP_DIR and DOMAIN."
