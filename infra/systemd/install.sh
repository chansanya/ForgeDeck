#!/usr/bin/env bash
# 将冻结依赖的 API、Runner、前端和模板安装到固定的 systemd 生产目录。
set -euo pipefail

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if (( $# != 0 )); then
  echo "Custom install roots are not supported because the systemd units use /opt/light-devops." >&2
  exit 2
fi
(( EUID == 0 )) || { echo "Run this installer as root." >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "Only Linux is supported." >&2; exit 1; }
machine_arch=$(uname -m)
[[ "$machine_arch" == x86_64 || "$machine_arch" == amd64 ]] || {
  echo "Only Linux amd64/x86_64 is supported." >&2
  exit 1
}
install_root=/opt/light-devops
config_root=/etc/light-devops
data_root=/var/lib/light-devops
log_root=/var/log/light-devops

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

version_at_least() {
  local version=${1#v}
  local major minor patch
  [[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)([-+][0-9A-Za-z.-]+)?$ ]] || return 1
  major=${BASH_REMATCH[1]}
  minor=${BASH_REMATCH[2]}
  patch=${BASH_REMATCH[3]}
  ((
    10#$major > $2
    || (10#$major == $2 && 10#$minor > $3)
    || (10#$major == $2 && 10#$minor == $3 && 10#$patch >= $4)
  ))
}

require_command git "git is required"
require_command uv "uv >= 0.11.16 is required"
require_command node "Node.js 24 LTS is required to build the web UI"
require_command corepack "Corepack is required"
require_command docker "Docker Engine CLI is required"
require_command systemctl "systemd is required"
[[ -f "$source_root/backend/uv.lock" ]] || fail "backend/uv.lock is required"
[[ -f "$source_root/package.json" ]] || fail "package.json is required"
[[ -f "$source_root/pnpm-lock.yaml" ]] || fail "pnpm-lock.yaml is required"
[[ -f "$source_root/pnpm-workspace.yaml" ]] || fail "pnpm-workspace.yaml is required"

node_version=$(node --version)
[[ "${node_version#v}" =~ ^24\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] \
  || fail "Node.js 24 LTS is required; found $node_version"

if ! pnpm_version=$(cd "$source_root" && corepack pnpm --version); then
  fail "pnpm 10.14.0 could not be started through Corepack"
fi
[[ "$pnpm_version" == 10.14.0 ]] || fail "pnpm 10.14.0 is required; found $pnpm_version"

uv_output=$(uv --version)
uv_version=${uv_output#uv }
uv_version=${uv_version%% *}
version_at_least "$uv_version" 0 11 16 || fail "uv >= 0.11.16 is required; found $uv_output"

docker buildx version >/dev/null 2>&1 || fail "Docker Buildx is required"
if ! compose_version=$(docker compose version --short); then
  fail "Docker Compose >= 2.20.0 is required"
fi
version_at_least "$compose_version" 2 20 0 \
  || fail "Docker Compose >= 2.20.0 is required; found $compose_version"
getent group docker >/dev/null || fail "The docker group is required"

getent group light-devops >/dev/null || groupadd --system light-devops
id devops-api >/dev/null 2>&1 || useradd --system --gid light-devops --home-dir "$install_root" --shell /usr/sbin/nologin devops-api
id devops-runner >/dev/null 2>&1 || useradd --system --gid light-devops --groups docker --home-dir "$install_root" --shell /usr/sbin/nologin devops-runner

install -d -o root -g light-devops -m 0750 "$install_root" "$config_root"
install -d -o devops-api -g light-devops -m 2770 "$data_root" "$log_root"
install -d -o root -g light-devops -m 0750 \
  "$install_root/backend" \
  "$install_root/frontend" \
  "$install_root/skills" \
  "$install_root/templates"
cp -a \
  "$source_root/backend/pyproject.toml" \
  "$source_root/backend/uv.lock" \
  "$source_root/backend/alembic.ini" \
  "$source_root/backend/README.md" \
  "$source_root/backend/src" \
  "$source_root/backend/alembic" \
  "$install_root/backend"/
cp -a \
  "$source_root/package.json" \
  "$source_root/pnpm-lock.yaml" \
  "$source_root/pnpm-workspace.yaml" \
  "$install_root"/
cp -a \
  "$source_root/frontend/package.json" \
  "$source_root/frontend/index.html" \
  "$source_root/frontend/vite.config.ts" \
  "$source_root/frontend/tsconfig.json" \
  "$source_root/frontend/tsconfig.app.json" \
  "$source_root/frontend/tsconfig.node.json" \
  "$source_root/frontend/src" \
  "$install_root/frontend"/
cp -a "$source_root/skills/." "$install_root/skills/"
cp -a "$source_root/templates/." "$install_root/templates/"
if [[ -d "$source_root/frontend/public" ]]; then
  cp -a "$source_root/frontend/public" "$install_root/frontend"/
fi
cd "$install_root/backend"
uv sync --frozen --no-dev --extra runner
if [[ ! -f "$config_root/master.key" ]]; then
  key_value=$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  install -o root -g light-devops -m 0640 /dev/null "$config_root/master.key"
  printf '%s\n' "$key_value" > "$config_root/master.key"
  unset key_value
fi
cd "$install_root"
corepack enable
pnpm install --frozen-lockfile --filter devops-console-web
pnpm --filter devops-console-web build

install -m 0644 "$source_root/infra/systemd/devops-api.service" /etc/systemd/system/devops-api.service
install -m 0644 "$source_root/infra/systemd/devops-runner.service" /etc/systemd/system/devops-runner.service
if [[ ! -f "$config_root/devops.env" ]]; then
  install -o root -g light-devops -m 0640 \
    "$source_root/infra/systemd/devops.env.example" \
    "$config_root/devops.env"
fi
chown -R root:light-devops "$install_root/backend" "$install_root/skills" "$install_root/templates"
chown -R devops-api:light-devops "$install_root/frontend"
chown -R devops-api:light-devops "$data_root" "$log_root"
chmod 2770 "$data_root" "$log_root"
systemctl daemon-reload
echo "Edit $config_root/devops.env, then enable devops-api and devops-runner."
