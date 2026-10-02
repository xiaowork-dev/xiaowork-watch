#!/usr/bin/env bash
set -Eeuo pipefail

# Installs the frontend prototype only. No monitor backend or VPS agent yet.
deploy_root=/opt/xiaowork-watch
listen_port=8088
server_name=_
action=menu
non_interactive=false
confirm_uninstall=false
show_after_install=false
usage() {
 cat <<'HELP'
用法：sudo bash install.sh [--port 8088] [--domain watch.example.com] [--path /opt/xiaowork-watch]
默认打开中文菜单：首次部署/卸载，安装后打开管理菜单。
--install / --non-interactive 直接部署或更新，不打开菜单。
--uninstall  卸载服务入口并保留下载数据；无终端时还需 --confirm。
--menu       打开菜单（默认）；--help 显示说明。
仅支持 Ubuntu/Debian，目前部署前端原型，自动更新约每15分钟检查一次。
手动源码部署仍可用，详见 docs/deployment/server.md。
HELP
}
while (($#)); do
 case "$1" in
  --port|--domain|--path)
   (($# >= 2)) || { usage; exit 2; }
   case "$1" in --port) listen_port=$2;; --domain) server_name=$2;; --path) deploy_root=$2;; esac
   shift 2;;
  -h|--help) usage; exit 0;;
  --install) action=deploy; shift;;
  --uninstall) action=uninstall; shift;;
  --menu) action=menu; shift;;
  --non-interactive) non_interactive=true; shift;;
  --confirm) confirm_uninstall=true; shift;;
  *) usage; exit 2;;
 esac
done
[[ "$non_interactive" != true || "$action" != menu ]] || action=deploy
[[ "$confirm_uninstall" != true || "$action" == uninstall ]] || { usage; exit 2; }
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
installed=false
if [[ -e "$marker" ]]; then
 [[ -f "$marker" ]] || { echo 'Unknown installation marker.' >&2; exit 1; }
 [[ "$(cat "$marker")" == xiaowork-watch-managed-v1 ]] || { echo 'Unknown installation marker.' >&2; exit 1; }
 if [[ -f "$completion" && "$(cat "$completion")" == xiaowork-watch-managed-v1 && -f "$deploy_root/current/.deploy/manage.py" ]]; then
  installed=true
 fi
elif [[ -d "$deploy_root" && -n "$(find "$deploy_root" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
 echo 'Installation directory is not empty. Use a new --path; existing files are unchanged.' >&2
 exit 1
fi
nginx_config=/etc/nginx/conf.d/xiaowork-watch.conf
wrapper=/usr/local/bin/xiaowork-watch
service=/etc/systemd/system/xiaowork-watch-update.service
timer=/etc/systemd/system/xiaowork-watch-update.timer
open_menu_terminal() {
 if ! { exec 3<>/dev/tty; } 2>/dev/null; then
  printf '%s\n' '未检测到交互终端。请在 SSH 终端运行；无人值守部署可加 --non-interactive。' >&2
  return 2
 fi
}
show_initial_menu() {
 open_menu_terminal || return $?
 while true; do
  printf '\n========== xiaowork Watch ==========\n当前未部署\n\n  1. 部署\n  2. 卸载\n  0. 退出\n\n请选择 [0-2]：' >&3
  if ! IFS= read -r -u 3 selection; then action=exit; return 0; fi
  case "$selection" in
   1) action=deploy; show_after_install=true; return 0;;
   2) action=uninstall; return 0;;
   0|'') action=exit; return 0;;
   *) printf '请输入 0、1 或 2。\n' >&3;;
  esac
 done
}
load_latest_scripts() {
 python3 - "$1" <<'PY'
import json, pathlib, re, sys, urllib.request
repo = 'xiaowork-dev/xiaowork-watch'
def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'xiaowork-watch-installer', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise SystemExit('管理工具下载内容超出限制。')
    return raw
release = json.loads(fetch('https://api.github.com/repos/' + repo + '/releases/latest'))
tag = release.get('tag_name', '')
if not re.fullmatch(r'web-[0-9a-f]{40}', tag) or release.get('draft') is not False or release.get('prerelease') is not False:
    raise SystemExit('尚无完整的前端发布版本，请等待 GitHub Actions 发布成功后再试。')
names = {a.get('name') for a in release.get('assets', []) if a.get('state') == 'uploaded'}
if not {'xiaowork-watch-web.tar.gz', 'xiaowork-watch-web.tar.gz.sha256'} <= names:
    raise SystemExit('发布包未上传完成，请稍后重试。')
folder = pathlib.Path(sys.argv[1])
folder.joinpath('tag').write_text(tag, encoding='utf-8')
for name in ('manage.py', 'console.py'):
    folder.joinpath(name).write_bytes(fetch('https://raw.githubusercontent.com/' + repo + '/' + tag[4:] + '/scripts/deploy/' + name))
PY
}
run_management() (
 if [[ -e "$deploy_root/control" || -L "$deploy_root/control" ]]; then
  [[ -L "$deploy_root/control" ]] || { echo '管理入口不是受控的版本链接，已停止。' >&2; exit 1; }
  control_target=$(realpath -e -- "$deploy_root/control")
  control_relative=${control_target#"$deploy_root/releases/"}
  [[ "$control_target" == "$deploy_root/releases/"* && "$control_relative" =~ ^[0-9a-f]{40}/\.deploy$ ]] || { echo '管理入口指向其他目录，已停止。' >&2; exit 1; }
  for tool_file in manage.py console.py; do
   [[ -f "$control_target/$tool_file" && ! -L "$control_target/$tool_file" ]] || { echo '管理工具缺失或类型无效。' >&2; exit 1; }
  done
  exec python3 "$control_target/manage.py" --root "$deploy_root" "$@"
 fi
 if [[ -f "$deploy_root/current/.deploy/manage.py" && -f "$deploy_root/current/.deploy/console.py" ]]; then
  exec python3 "$deploy_root/current/.deploy/manage.py" --root "$deploy_root" "$@"
 fi
 # Older releases do not understand menu/uninstall. Load tools from a pinned
 # published commit without changing the site's version or update preference.
 management_temp=$(mktemp -d /tmp/xiaowork-watch-menu.XXXXXXXX)
 cleanup_management() {
  case "$management_temp" in /tmp/xiaowork-watch-menu.*) [[ -d "$management_temp" && ! -L "$management_temp" ]] && rm -rf -- "$management_temp";; esac
 }
 trap cleanup_management EXIT
 printf '正在获取新版管理菜单，当前网站版本保持不变。\n'
 load_latest_scripts "$management_temp"
 python3 "$management_temp/manage.py" --root "$deploy_root" "$@"
)
if [[ "$action" == menu ]]; then
 if [[ "$installed" == true ]]; then open_menu_terminal; else show_initial_menu; fi
fi
[[ "$action" != exit ]] || exit 0
[[ ${EUID} -eq 0 ]] || { echo '请使用 sudo/root 运行部署管理操作。' >&2; exit 1; }
[[ -f /etc/os-release ]] || { echo '需要 Ubuntu/Debian 系统。' >&2; exit 1; }
. /etc/os-release
[[ ${ID:-} == ubuntu || ${ID:-} == debian ]] || { echo '支持的系统：Ubuntu、Debian。' >&2; exit 1; }
if [[ "$action" == uninstall ]]; then
 if [[ ! -f "$marker" ]]; then printf '此目录没有本项目的部署记录，无需卸载。\n'; exit 0; fi
 if [[ "$confirm_uninstall" != true ]]; then
  open_menu_terminal
  printf '\n将移除本项目的站点配置、反代和自动更新入口。\n历史发布包保留在 %s；Nginx 和其他站点保留。\n输入 UNINSTALL 确认，其他输入取消：' "$deploy_root" >&3
  IFS= read -r -u 3 confirmation || exit 0
  [[ "$confirmation" == UNINSTALL ]] || { printf '已取消卸载。\n' >&3; exit 0; }
 fi
 run_management uninstall --confirm
 exit $?
fi
if [[ "$installed" == true ]]; then
 if [[ "$action" == menu ]]; then run_management menu; else python3 "$deploy_root/current/.deploy/manage.py" --root "$deploy_root" update; fi
 exit $?
fi
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
load_latest_scripts "$install_temp"
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
# xiaowork-watch-root: $deploy_root
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
# Managed by xiaowork Watch's frontend installer.
# xiaowork-watch-root: $deploy_root
exec /usr/bin/python3 "$deploy_root/control/manage.py" --root "$deploy_root" "\$@"
WRAPPER
chmod 755 "$wrapper"
[[ ! -e "$service" && ! -L "$service" ]] || { echo 'Service path became occupied.' >&2; exit 1; }
service_written=true
cat > "$service" <<SERVICE
# Managed by xiaowork Watch's frontend installer.
# xiaowork-watch-root: $deploy_root
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
cat > "$timer" <<TIMER
# Managed by xiaowork Watch's frontend installer.
# xiaowork-watch-root: $deploy_root
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
printf '\n部署完成，端口 %s。当前为前端原型，真实监控后端与 VPS 探针尚未接入。\n' "$listen_port"
printf '访问 http://服务器IP:%s/ ，如有防火墙或云安全组请开放此端口。\n' "$listen_port"
printf '管理菜单：sudo xiaowork-watch\n命令：sudo xiaowork-watch update | rollback | status\n'
printf '约每15分钟检查 GitHub 更新；手动回退会暂停自动更新。\n'
if [[ "$show_after_install" == true ]]; then run_management menu; fi
