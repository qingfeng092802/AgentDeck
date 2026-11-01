#!/bin/sh
# AgentDeck 容器启动：等库 → 迁移 → 起看板与 API。
#
# 为什么迁移放在这里而不是 compose 的 `command`：`depends_on: condition: service_healthy`
# 只保证"库里能接连接"，不保证"库刚起来还在 initdb 收尾"；而且 `docker run`（不走 compose）
# 时没有任何人在前面排序。等库 + 迁移 + 起服务必须在同一个进程序列里，失败才能把容器退掉。
set -eu

HOST="${ACP_HOST:-0.0.0.0}"
PORT="${ACP_PORT:-8765}"
BUS_ROOT="${ACP_RUNTIME_BUS_ROOT:-/app/results}"
DB_WAIT_SECONDS="${ACP_DB_WAIT_SECONDS:-60}"

log() { printf '%s [acp] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

if [ ! -f /app/alembic.ini ]; then
    log "缺少 /app/alembic.ini ⇒ 这个镜像不是按仓库根构建的，退出"
    exit 1
fi

log "等待数据库（最多 ${DB_WAIT_SECONDS}s）"
python - "$DB_WAIT_SECONDS" <<'PY'
import os
import sys
import time

import psycopg

wait_seconds = float(sys.argv[1])
dsn = os.environ.get("ACP_DB_DSN", "")
if not dsn:
    raise SystemExit("[acp] ACP_DB_DSN 未注入 ⇒ 迁移与落库都无从谈起（cp .env.example .env）")

deadline = time.monotonic() + wait_seconds
last = "（一次都没连上）"
while time.monotonic() < deadline:
    try:
        connection = psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - 起服务前的等待，任何异常都只说明"还没好"
        last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
        continue
    connection.close()
    print("[acp] 数据库已可达", file=sys.stderr)
    raise SystemExit(0)

raise SystemExit(f"[acp] {wait_seconds:.0f}s 内没连上数据库，最后一次错误：{last}")
PY

log "alembic upgrade head"
alembic upgrade head

# 两个可选名单：不给就保持"看板能看不能批"（api/approvals.py 的 503），这是刻意的默认。
# 名单项是身份名（不是路径），逗号分隔即可，所以这里按空白切分是安全的。
if [ -n "${ACP_APPROVERS:-}" ]; then
    for principal in $(printf '%s' "$ACP_APPROVERS" | tr ',' ' '); do
        set -- "$@" --approver "$principal"
    done
fi
if [ -n "${ACP_MACHINE_PRINCIPALS:-}" ]; then
    for principal in $(printf '%s' "$ACP_MACHINE_PRINCIPALS" | tr ',' ' '); do
        set -- "$@" --machine-principal "$principal"
    done
fi

log "启动 acp.serve：${HOST}:${PORT} · 静态面 /app/web/dist · 总线根 ${BUS_ROOT}"
exec python -m acp.serve \
    --host "$HOST" \
    --port "$PORT" \
    --dist /app/web/dist \
    --bus-root "$BUS_ROOT" \
    --log-level "${ACP_LOG_LEVEL:-info}" \
    "$@"
