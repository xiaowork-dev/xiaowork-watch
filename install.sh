#!/usr/bin/env bash
set -Eeuo pipefail

# Installs the frontend prototype only. No monitor backend or VPS agent yet.
deploy_root=/opt/xiaowork-watch
listen_port=8088
server_name=_
usage() {
 cat <<'HELP'
Usage: sudo bash install.sh [--port 8088] [--domain watch.example.com] [--path /opt/xiaowork-watch]
Ubuntu/Debian only. Installs the frontend prototype and checks GitHub every 15 minutes.
Manual source deployment is also supported; see docs/deployment/server.md.
HELP
}
while (($#)); do
 case "$1" in
  --port|--domain|--path)
   (($# >= 2)) || { usage; exit 2; }
   case "$1" in --port) listen_port=$2;; --domain) server_name=$2;; --path) deploy_root=$2;; esac
   shift 2;;
  -h|--help) usage; exit 0;;
  *) usage; exit 2;;
 esac
done
[[ ${EUID} -eq 0 ]] || { echo 'Run with sudo/root.' >&2; exit 1; }
[[ -f /etc/os-release ]] || { echo 'Ubuntu/Debian is required.' >&2; exit 1; }
. /etc/os-release
[[ ${ID:-} == ubuntu || ${ID:-} == debian ]] || { echo 'Supported systems: Ubuntu and Debian.' >&2; exit 1; }
[[ "$listen_port" =~ ^[0-9]{1,5}$ ]] && ((10#$listen_port >= 1 && 10#$listen_port <= 65535)) || { echo 'Invalid port.' >&2; exit 2; }
[[ "$server_name" == _ || "$server_name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo 'Invalid domain.' >&2; exit 2; }
[[ "$deploy_root" =~ ^/[A-Za-z0-9._/-]+$ && "$deploy_root" != / && "$deploy_root" != */../* && "$deploy_root" != */.. && "$deploy_root" != */./* ]] || { echo 'Invalid installation path.' >&2; exit 2; }
path_probe=$deploy_root
while [[ "$path_probe" != / && "$path_probe" == */ ]]; do path_probe=${path_probe%/}; done
while [[ -n "$path_probe" && "$path_probe" != / ]]; do
 [[ ! -L "$path_probe" ]] || { echo 'Installation path must not contain symlinks.' >&2; exit 2; }
 path_probe=$(dirname -- "$path_probe")
done
deploy_root=$(realpath -m -- "$deploy_root")
[[ "$deploy_root" != / && (! -e "$deploy_root" || -d "$deploy_root") ]] || { echo 'Unsafe installation path.' >&2; exit 2; }
marker="$deploy_root/.xiaowork-watch-managed"
completion="$deploy_root/.installation-complete"
[[ ! -L "$marker" && ! -L "$completion" ]] || { echo 'Unsafe installation marker.' >&2; exit 1; }
if [[ -e "$marker" ]]; then
 [[ -f "$marker" ]] || { echo 'Unknown installation marker.' >&2; exit 1; }
 [[ "$(cat "$marker")" == xiaowork-watch-managed-v1 ]] || { echo 'Unknown installation marker.' >&2; exit 1; }
 if [[ -f "$completion" && "$(cat "$completion")" == xiaowork-watch-managed-v1 && -f "$deploy_root/current/.deploy/manage.py" ]]; then
  exec python3 "$deploy_root/current/.deploy/manage.py" --root "$deploy_root" update
 fi
elif [[ -d "$deploy_root" && -n "$(find "$deploy_root" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
 echo 'Installation directory is not empty. Use a new --path; existing files are unchanged.' >&2
 exit 1
fi
nginx_config=/etc/nginx/conf.d/xiaowork-watch.conf
wrapper=/usr/local/bin/xiaowork-watch
service=/etc/systemd/system/xiaowork-watch-update.service
timer=/etc/systemd/system/xiaowork-watch-update.timer
for reserved in "$nginx_config" "$wrapper" "$service" "$timer"; do
 [[ ! -e "$reserved" && ! -L "$reserved" ]] || { echo "Existing configuration found: $reserved. Review it before installation." >&2; exit 1; }
done
command -v systemctl >/dev/null || { echo 'systemd is required.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx python3 curl ca-certificates

install_temp=$(mktemp -d /tmp/xiaowork-watch-install.XXXXXXXX)
install_success=false
nginx_written=false
wrapper_written=false
service_written=false
timer_written=false
cleanup() {
 result=$?
 trap - EXIT
 set +e
 if [[ "$install_success" != true ]]; then
  if [[ "$timer_written" == true ]]; then
   systemctl disable --now xiaowork-watch-update.timer >/dev/null 2>&1 || true
   [[ ! -L "$timer" ]] && rm -f -- "$timer"
  fi
  [[ "$service_written" != true || -L "$service" ]] || rm -f -- "$service"
  [[ "$wrapper_written" != true || -L "$wrapper" ]] || rm -f -- "$wrapper"
  if [[ "$nginx_written" == true && ! -L "$nginx_config" ]]; then
   rm -f -- "$nginx_config"
   nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  echo 'Installation failed. Configuration created by this run was removed; downloaded files remain for retry.' >&2
 fi
 case "$install_temp" in /tmp/xiaowork-watch-install.*) [[ -d "$install_temp" && ! -L "$install_temp" ]] && rm -rf -- "$install_temp";; esac
 exit "$result"
}
trap cleanup EXIT
python3 - "$install_temp" <<'PY'
import json, pathlib, re, sys, urllib.request
repo = 'xiaowork-dev/xiaowork-watch'
def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'xiaowork-watch-installer', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(2 * 1024 * 1024)
release = json.loads(fetch('https://api.github.com/repos/' + repo + '/releases/latest'))
tag = release.get('tag_name', '')
if not re.fullmatch(r'web-[0-9a-f]{40}', tag) or release.get('draft') or release.get('prerelease'):
    raise SystemExit('No supported frontend release is available; no website configuration was changed.')
names = {a.get('name') for a in release.get('assets', []) if a.get('state') == 'uploaded'}
if not {'xiaowork-watch-web.tar.gz', 'xiaowork-watch-web.tar.gz.sha256'} <= names:
    raise SystemExit('The frontend release is incomplete; try again after its build finishes.')
folder = pathlib.Path(sys.argv[1])
folder.joinpath('tag').write_text(tag, encoding='utf-8')
folder.joinpath('manage.py').write_bytes(fetch('https://raw.githubusercontent.com/' + repo + '/' + tag[4:] + '/scripts/deploy/manage.py'))
PY
for managed_dir in "$deploy_root/releases" "$deploy_root/shared" "$deploy_root/shared/assets"; do
 [[ ! -L "$managed_dir" && (! -e "$managed_dir" || -d "$managed_dir") ]] || { echo "Unsafe managed directory: $managed_dir" >&2; exit 1; }
done
mkdir -p "$deploy_root/releases" "$deploy_root/shared/assets"
printf '%s\n' xiaowork-watch-managed-v1 > "$marker"
chmod 755 "$deploy_root" "$deploy_root/releases" "$deploy_root/shared" "$deploy_root/shared/assets"
python3 - "$deploy_root" "$listen_port" "$server_name" <<'PY'
import json, pathlib, sys
folder = pathlib.Path(sys.argv[1])
config = folder / 'config.json'
if config.is_symlink():
    raise SystemExit('Config must not be a symlink.')
config.write_text(json.dumps({'healthUrl': 'http://127.0.0.1:' + sys.argv[2] + '/release.json', 'healthHost': '' if sys.argv[3] == '_' else sys.argv[3], 'autoUpdate': True}, indent=2) + '\n', encoding='utf-8')
config.chmod(0o600)
PY
[[ ! -e "$nginx_config" && ! -L "$nginx_config" ]] || { echo 'Nginx configuration path became occupied.' >&2; exit 1; }
nginx_written=true
cat > "$nginx_config" <<NGINX
# Managed by xiaowork Watch's frontend installer.
server {
    listen $listen_port;
    server_name $server_name;
    root $deploy_root/current;
    index index.html;
    location /assets/ {
        alias $deploy_root/shared/assets/;
        add_header Cache-Control "public, max-age=31536000, immutable" always;
    }
    location = /index.html {
        add_header Cache-Control "no-cache" always;
    }
    location = /release.json {
        add_header Cache-Control "no-store" always;
    }
    location ~ /\. { deny all; }
    location / { try_files \$uri \$uri/ /index.html; }
}
NGINX
if ! nginx -t; then
 echo 'Nginx configuration check failed.' >&2
 exit 1
fi
systemctl start nginx
systemctl reload nginx
python3 "$install_temp/manage.py" --root "$deploy_root" install --release-tag "$(cat "$install_temp/tag")"
[[ ! -e "$wrapper" && ! -L "$wrapper" ]] || { echo 'Command path became occupied.' >&2; exit 1; }
wrapper_written=true
cat > "$wrapper" <<WRAPPER
#!/bin/sh
exec /usr/bin/python3 "$deploy_root/current/.deploy/manage.py" --root "$deploy_root" "\$@"
WRAPPER
chmod 755 "$wrapper"
[[ ! -e "$service" && ! -L "$service" ]] || { echo 'Service path became occupied.' >&2; exit 1; }
service_written=true
cat > "$service" <<SERVICE
[Unit]
Description=Check GitHub for xiaowork Watch frontend updates
Wants=network-online.target
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xiaowork-watch update --automatic
TimeoutStartSec=300
SERVICE
[[ ! -e "$timer" && ! -L "$timer" ]] || { echo 'Timer path became occupied.' >&2; exit 1; }
timer_written=true
cat > "$timer" <<'TIMER'
[Unit]
Description=Automatically update xiaowork Watch frontend from GitHub

[Timer]
OnBootSec=5min
OnUnitInactiveSec=15min
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable nginx
systemctl enable --now xiaowork-watch-update.timer
printf '%s\n' xiaowork-watch-managed-v1 > "$completion"
install_success=true
printf '\nInstalled frontend prototype on port %s. No real monitor backend or VPS agent is included.\n' "$listen_port"
printf 'Open http://YOUR_SERVER_IP:%s/ (allow this port in your server firewall if required).\n' "$listen_port"
printf 'Commands: sudo xiaowork-watch update | sudo xiaowork-watch rollback | sudo xiaowork-watch status\n'
printf 'GitHub updates are checked about every 15 minutes. Rollback pauses automatic updates.\n'
