#!/usr/bin/env bash
# Runs on the server: pull the latest pushed commit and restart the backend only when needed.
# deploy.ps1 copies this file to /tmp and runs it there with sudo, so a pull that changes
# this script never edits it while it is running. It can also be run by hand:
#   sudo bash /opt/webae/deploy/update.sh
set -euo pipefail

APP=/opt/webae
SERVICE=aeweb
PORT=8765

as_app() { sudo -u aeweb -H "$@"; }

cd "$APP"
old=$(as_app git rev-parse HEAD)
as_app git pull --ff-only --quiet
new=$(as_app git rev-parse HEAD)

if [[ $old == "$new" ]]; then
  echo "服务器已经是最新版本：$(as_app git log -1 --format='%h %s')"
  exit 0
fi

echo "更新 ${old:0:7} -> ${new:0:7}："
as_app git log --format='  %h %s' "$old..$new"
changed=$(as_app git diff --name-only "$old" "$new")

if grep -qE '^backend/(pyproject\.toml|uv\.lock)$' <<<"$changed"; then
  echo "依赖有变化，正在同步…"
  (cd backend && as_app uv sync --frozen)
fi

if grep -qE '^backend/[^/]*\.py$|^backend/(pyproject\.toml|uv\.lock)$' <<<"$changed"; then
  echo "后端代码有变化，重启服务…"
  systemctl restart "$SERVICE"
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      echo "后端已恢复（${i} 秒）"
      exit 0
    fi
    sleep 1
  done
  echo "后端 30 秒内没有起来，最近的日志："
  journalctl -u "$SERVICE" -n 30 --no-pager
  exit 1
else
  echo "只改了网页、文档或 OC 脚本，不需要重启，刷新网页即可。"
fi
