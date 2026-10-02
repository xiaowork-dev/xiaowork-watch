#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

AGENT_USER=xiaowork-watch-agent
APP_DIR=/opt/xiaowork-watch-agent
CONFIG_DIR=/etc/xiaowork-watch-agent
UNIT=/etc/systemd/system/xiaowork-watch-agent.service
UNIT_NAME=xiaowork-watch-agent.service
OWNER=xiaowork-watch-agent-managed-v1
server= token= expected_sha= development=false uninstall=false
temporary= created_user=false created_group=false created_app=false created_config=false created_unit=false success=false

die() { printf '%s\n' "$1" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --server|--enrollment-token|--agent-sha)
            (($# >= 2)) || die 'Missing installation parameter.'
            case "$1" in
                --server) server=$2 ;;
                --enrollment-token) token=$2 ;;
                --agent-sha) expected_sha=$2 ;;
            esac
            shift 2 ;;
        --development) development=true; shift ;;
        --uninstall) uninstall=true; shift ;;
        *) die 'Unknown installation option.' ;;
    esac
done

[[ $EUID == 0 ]] || die 'Run this installer with sudo or as root.'
[[ $(uname -s) == Linux ]] || die 'This agent requires Linux.'
[[ -f /etc/os-release ]] || die 'Unable to identify the Linux distribution.'
. /etc/os-release
[[ ${ID:-} == ubuntu || ${ID:-} == debian ]] || die 'This installer supports Ubuntu and Debian.'

check_path() {
    local current= part
    local -a parts
    IFS=/ read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do
        [[ -n $part ]] || continue
        current+=/$part
        [[ ! -L $current ]] || die 'Refusing a symbolic link in an agent installation path.'
    done
}

for path in "$APP_DIR" "$CONFIG_DIR" "$UNIT"; do check_path "$path"; done

unit_text() {
    cat <<'UNIT_EOF'
# xiaowork-watch-agent-managed-v1
[Unit]
Description=xiaowork Watch outbound heartbeat and ICMP agent
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=xiaowork-watch-agent
Group=xiaowork-watch-agent
ExecStart=/usr/bin/python3 -I -B /opt/xiaowork-watch-agent/agent.py --config /etc/xiaowork-watch-agent/config.json
Restart=always
RestartSec=10
TimeoutStopSec=15
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT_EOF
}

owned_marker() {
    printf '%s\nuid=%s\ngid=%s\n' "$OWNER" "$agent_uid" "$agent_gid"
}

cleanup() {
    local result=$?
    trap - EXIT
    if [[ $success != true ]]; then
        if [[ $created_unit == true ]]; then
            systemctl disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
            rm -f -- "$UNIT"
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        if [[ $created_config == true ]]; then
            rm -f -- "$CONFIG_DIR/config.json" "$CONFIG_DIR/.managed"
            rmdir -- "$CONFIG_DIR" 2>/dev/null || true
        fi
        if [[ $created_app == true ]]; then
            rm -f -- "$APP_DIR/agent.py" "$APP_DIR/.managed"
            rmdir -- "$APP_DIR" 2>/dev/null || true
        fi
        [[ $created_user != true ]] || userdel "$AGENT_USER" >/dev/null 2>&1 || true
        [[ $created_group != true ]] || groupdel "$AGENT_USER" >/dev/null 2>&1 || true
    fi
    if [[ -n $temporary ]]; then
        rm -f -- "$temporary/agent.py" "$temporary/agent.py.sha256" "$temporary/config.json"
        rmdir -- "$temporary" 2>/dev/null || true
    fi
    exit "$result"
}
trap cleanup EXIT

if [[ $uninstall == true ]]; then
    [[ -z $server && -z $token && -z $expected_sha && $development == false ]] || die 'Uninstall cannot be combined with enrollment options.'
    [[ -d $APP_DIR && -d $CONFIG_DIR && -f $UNIT && -f $APP_DIR/agent.py && -f $CONFIG_DIR/config.json ]] || die 'A complete owned agent installation was not found; nothing was removed.'
    passwd_entry=$(getent passwd "$AGENT_USER") || die 'Agent account is missing; ownership cannot be verified.'
    IFS=: read -r name password agent_uid agent_gid comment home shell <<< "$passwd_entry"
    [[ $name == "$AGENT_USER" && $comment == 'xiaowork Watch outbound agent' && $home == /nonexistent && $shell == /usr/sbin/nologin ]] || die 'The existing account is not owned by this installer.'
    group_entry=$(getent group "$AGENT_USER") || die 'Agent group is missing.'
    IFS=: read -r group_name group_password group_gid members <<< "$group_entry"
    [[ $group_name == "$AGENT_USER" && $group_gid == "$agent_gid" && -z $members ]] || die 'The agent group has unexpected ownership or members.'
    for directory in "$APP_DIR" "$CONFIG_DIR"; do
        [[ $(stat -c %u "$directory") == 0 ]] || die 'The agent directory is not root-owned.'
        [[ -f $directory/.managed && ! -L $directory/.managed && $(stat -c %u "$directory/.managed") == 0 ]] || die 'Agent ownership marker is missing or unsafe.'
        cmp -s "$directory/.managed" <(owned_marker) || die 'The installation belongs to a different account generation.'
        shopt -s dotglob nullglob
        for entry in "$directory"/*; do
            [[ -f $entry && ! -L $entry && $(stat -c %u "$entry") == 0 ]] || die 'Unexpected files were found; nothing was removed.'
            case "$entry" in
                "$APP_DIR/agent.py"|"$APP_DIR/.managed"|"$CONFIG_DIR/config.json"|"$CONFIG_DIR/.managed") ;;
                *) die 'Unexpected data was found in an agent directory; nothing was removed.' ;;
            esac
        done
    done
    [[ $(stat -c %u "$UNIT") == 0 ]] && cmp -s "$UNIT" <(unit_text) || die 'The service unit is not owned by this installer.'
    systemctl disable --now "$UNIT_NAME"
    rm -- "$UNIT"
    systemctl daemon-reload
    rm -- "$APP_DIR/agent.py" "$APP_DIR/.managed" "$CONFIG_DIR/config.json" "$CONFIG_DIR/.managed"
    rmdir -- "$APP_DIR" "$CONFIG_DIR"
    userdel "$AGENT_USER"
    groupdel "$AGENT_USER"
    success=true
    printf '%s\n' 'xiaowork Watch agent removed. Controller records and apt packages are retained.'
    exit 0
fi

[[ $token =~ ^[A-Za-z0-9_-]{16,512}$ ]] || die 'A valid enrollment token is required.'
[[ $expected_sha =~ ^[a-f0-9]{64}$ ]] || die 'The installation command must include --agent-sha (generate a new command in the controller).'
server=${server%/}
if [[ $server =~ ^https://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:[0-9]+)?$ ]]; then
    protocol=https
elif [[ $development == true && $server =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?$ ]]; then
    protocol=http
else
    die 'Use an HTTPS control server origin. HTTP requires --development and localhost.'
fi

for path in "$APP_DIR" "$CONFIG_DIR" "$UNIT"; do
    [[ ! -e $path && ! -L $path ]] || die 'An agent path already exists. Nothing was overwritten; uninstall the existing owned agent first.'
done
! getent passwd "$AGENT_USER" >/dev/null || die 'The agent account already exists; nothing was overwritten.'
! getent group "$AGENT_USER" >/dev/null || die 'The agent group already exists; nothing was overwritten.'
command -v systemctl >/dev/null || die 'systemd is required.'
[[ -d /run/systemd/system ]] || die 'Run this installer on a server with systemd running.'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 curl iputils-ping ca-certificates
temporary=$(mktemp -d /tmp/xiaowork-watch-agent.XXXXXXXX)
curl -fsS --proto "=$protocol" --connect-timeout 10 --max-time 60 --retry 2 --max-filesize 262144 "$server/agent/agent.py" -o "$temporary/agent.py"
curl -fsS --proto "=$protocol" --connect-timeout 10 --max-time 30 --retry 2 --max-filesize 1024 "$server/agent/agent.py.sha256" -o "$temporary/agent.py.sha256"
checksum_line=$(cat "$temporary/agent.py.sha256")
[[ $checksum_line =~ ^([a-f0-9]{64})\ \ agent\.py$ ]] || die 'The controller returned an invalid source checksum.'
[[ ${BASH_REMATCH[1]} == "$expected_sha" ]] || die 'The controller source version changed. Generate a new installation command.'
actual_sha=$(sha256sum "$temporary/agent.py")
[[ ${actual_sha%% *} == "$expected_sha" ]] || die 'Agent source checksum verification failed.'
/usr/bin/python3 -I -B -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$temporary/agent.py"
arguments=(--enroll --server "$server" --token "$token" --config "$temporary/config.json")
[[ $development != true ]] || arguments+=(--development)
/usr/bin/python3 -I -B "$temporary/agent.py" "${arguments[@]}"
unset token arguments

groupadd --system "$AGENT_USER"
created_group=true
useradd --system --gid "$AGENT_USER" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --comment 'xiaowork Watch outbound agent' "$AGENT_USER"
created_user=true
agent_uid=$(id -u "$AGENT_USER")
agent_gid=$(id -g "$AGENT_USER")
mkdir -- "$APP_DIR"
created_app=true
chmod 755 "$APP_DIR"
owned_marker > "$APP_DIR/.managed"
install -o root -g root -m 644 "$temporary/agent.py" "$APP_DIR/agent.py"
mkdir -- "$CONFIG_DIR"
created_config=true
chown "root:$AGENT_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
owned_marker > "$CONFIG_DIR/.managed"
install -o root -g "$AGENT_USER" -m 640 "$temporary/config.json" "$CONFIG_DIR/config.json"
(set -o noclobber; unit_text > "$UNIT")
created_unit=true
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
systemctl is-active --quiet "$UNIT_NAME"
success=true
printf '%s\n' 'xiaowork Watch agent installed. It connects outbound; no inbound VPS ports are required.'
