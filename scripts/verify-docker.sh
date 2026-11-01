#!/usr/bin/env bash
# 干净环境验证：一条命令确认"docker compose up 之后看板真的能用"。
#
# 本机（Windows + 没有 docker CLI 的沙箱）跑不了 Docker，所以 P0-5 的验收不能在开发机上
# 只看代码；这个脚本把"验收"变成可复制的一件事：在一台装了 Docker Desktop / Docker Engine
# 的干净机器（Linux / macOS / WSL2）上执行它，全绿才算过。
#
#   bash scripts/verify-docker.sh            # 全跑一遍，成功后 down -v
#   bash scripts/verify-docker.sh --keep     # 成功后留着栈，方便手动点开看板
#   bash scripts/verify-docker.sh --port 8899  # 换宿主端口（默认 8765）
#   bash scripts/verify-docker.sh --py-base  registry.example.com/library/python:3.12-slim \
#                                --node-base registry.example.com/library/node:20-alpine
#                                            # 换基镜像源（内网/镜像站；默认官方名见 Dockerfile 的两个 ARG）
#   ACP_PULL_TIMEOUT=20 bash scripts/verify-docker.sh   # 拉镜像的等待上限（默认 60s，超时即失败）
#
# 它断的是"能看见的东西"：镜像里没有 .env、容器不是 root、库表真的建出来了（alembic 跑过）、
# 从宿主机能取到看板 HTML、API 返回 JSON、伪造 Host 被 400 挡住、发布端口仍在环回。
#
# 2026-10-31 补的那一格：**拉不到基镜像时快速失败，不把整条脚本拖死**。
# 现场是本机与 WSL 侧都 `docker manifest inspect` 不到 `node:20-alpine` / `python:3.12-slim`
# （Docker Hub 不可达），而旧脚本会直接进 `docker compose build` 卡在拉取上。现在先做一次
# "本地有没有 / 能不能在 N 秒内拉到"的预检：本地已有就用本地的，拉不到就在第 0 步停下并给出
# 换源写法 —— 验收没跑成是**明确失败**，不是挂着不动。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KEEP=0
PORT="${ACP_APP_PORT:-8765}"
PY_BASE="${ACP_PY_BASE:-python:3.12-slim}"
NODE_BASE="${ACP_NODE_BASE:-node:20-alpine}"
PULL_TIMEOUT="${ACP_PULL_TIMEOUT:-60}"
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --port) PORT="${2:?--port 需要一个端口号}"; shift ;;
        --py-base) PY_BASE="${2:?--py-base 需要一个镜像名}"; shift ;;
        --node-base) NODE_BASE="${2:?--node-base 需要一个镜像名}"; shift ;;
        --pull-timeout) PULL_TIMEOUT="${2:?--pull-timeout 需要秒数}"; shift ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else GREEN=''; RED=''; DIM=''; OFF=''; fi

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  %sPASS%s %s\n' "$GREEN" "$OFF" "$*"; }
bad() { FAIL=$((FAIL + 1)); printf '  %sFAIL%s %s\n' "$RED" "$OFF" "$*"; }
step() { printf '\n%s== %s%s\n' "$DIM" "$*" "$OFF"; }
die() { printf '%s提前中止：%s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        printf '\n%s栈保留着：docker compose down -v 可拆%s\n' "$DIM" "$OFF"
        return
    fi
    printf '\n%s拆除栈与卷…%s\n' "$DIM" "$OFF"
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap 'if [ "$FAIL" -gt 0 ]; then printf "\n%s失败 —— 保留栈与日志供排查：docker compose logs app%s\n" "$DIM" "$OFF"; fi' EXIT

step "0. 前置检查"
command -v docker >/dev/null 2>&1 || die "没有 docker CLI。装 Docker Desktop / Docker Engine 后重试"
docker info >/dev/null 2>&1 || die "docker daemon 没在跑（Docker Desktop 未启动？）"
docker compose version >/dev/null 2>&1 || die "缺 docker compose v2（本脚本用 'docker compose' 而不是 docker-compose）"
command -v curl >/dev/null 2>&1 || die "缺 curl（用来从宿主机取看板与 API）"
ok "docker $(docker version --format '{{.Server.Version}}') / compose $(docker compose version --format '{{.Version}}')"

step "1. .env"
if [ ! -f .env ]; then
    cp .env.example .env
    printf '%s已从 .env.example 生成 .env（没有凭据，只有空位）%s\n' "$DIM" "$OFF"
fi
if ! grep -qE '^ACP_PG_PASSWORD=.+' .env; then
    # 只在验证脚本自己造场景时补一个一次性口令；已有的 .env 不擅自改写。
    printf 'ACP_PG_PASSWORD=verify-%s\n' "$(date +%s)" >> .env
    printf '%s.env 里的 ACP_PG_PASSWORD 是空的 ⇒ 已补一个一次性口令（跑完可删）%s\n' "$DIM" "$OFF"
fi
if ! grep -qE '^ACP_PG_HOST_PORT=.+' .env; then
    printf 'ACP_PG_HOST_PORT=55432\n' >> .env
    printf '%s未设 ACP_PG_HOST_PORT ⇒ 写 55432，避开宿主原生 5432%s\n' "$DIM" "$OFF"
fi
ok ".env 就绪（PG 宿主端口 $(grep -E '^ACP_PG_HOST_PORT=' .env | cut -d= -f2)）"

step "1.5 基镜像（本地优先；拉不到就快速失败）"
ensure_base_image() {
    local image="$1" role="$2"
    if docker image inspect "$image" >/dev/null 2>&1; then
        ok "${role}基镜像本地已有：${image}"
        return 0
    fi
    printf '%s本地没有 %s，尝试拉取（上限 %ss）…%s\n' "$DIM" "$image" "$PULL_TIMEOUT" "$OFF"
    local rc=0
    if command -v timeout >/dev/null 2>&1; then
        timeout "$PULL_TIMEOUT" docker pull "$image" >/dev/null 2>&1 || rc=$?
    else
        docker pull "$image" >/dev/null 2>&1 || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
        ok "${role}基镜像已拉取：${image}"
        return 0
    fi
    printf '\n%s拉不到基镜像：%s（退出码 %s）%s\n' "$RED" "$image" "$rc" "$OFF" >&2
    printf '%s两条路：① 换一台能连镜像仓库的机器；② 换源重跑 ——%s\n' "$DIM" "$OFF" >&2
    printf '  bash scripts/verify-docker.sh --py-base <源>/python:3.12-slim --node-base <源>/node:20-alpine\n' >&2
    printf '%s本地已有镜像可用 `docker image ls` 查；改名前先确认它与官方包的发行版/依赖一致。%s\n' "$DIM" "$OFF" >&2
    exit 3
}
ensure_base_image "$NODE_BASE" "node（段一）"
ensure_base_image "$PY_BASE" "python（段二）"

step "2. 构建镜像"
docker compose build \
    --build-arg "PY_BASE=$PY_BASE" \
    --build-arg "NODE_BASE=$NODE_BASE" \
    app || die "构建失败（这一步会 npm ci + vite build + pip install -e .，日志见上）"
ok "镜像构建完成"

step "3. 镜像自身的三条静态事实"
# 3.1 凭据没有烘进镜像层
if docker run --rm --entrypoint sh acp-app:local -c 'test -f /app/.env' >/dev/null 2>&1; then
    bad "镜像里存在 /app/.env ⇒ .dockerignore 没挡住凭据"
else
    ok "镜像内无 .env"
fi
# 3.2 看板产物真的烘进去了
if docker run --rm --entrypoint sh acp-app:local -c 'test -f /app/web/dist/index.html' >/dev/null 2>&1; then
    ok "镜像内有 /app/web/dist/index.html"
else
    bad "镜像缺少看板产物 ⇒ 段一的 dist 没拷进段二"
fi
# 3.3 迁移脚本在镜像里（alembic.ini 指向 src/acp/persistence/migrations）
if docker run --rm --entrypoint sh acp-app:local -c 'test -f /app/alembic.ini && test -d /app/src/acp/persistence/migrations' >/dev/null 2>&1; then
    ok "镜像内有 alembic.ini 与迁移目录"
else
    bad "镜像缺少 alembic 配置或迁移目录 ⇒ 启动脚本的 upgrade 必挂"
fi

step "4. 起栈"
export ACP_APP_PORT="$PORT"
docker compose up -d || die "up 失败"
APP_ID="$(docker compose ps -q app)"
[ -n "$APP_ID" ] || die "没拿到 app 容器 id"

printf '%s等待 app 变 healthy（最多 180s）…%s\n' "$DIM" "$OFF"
status=""
for _ in $(seq 1 90); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$APP_ID" 2>/dev/null || echo missing)"
    [ "$status" = "healthy" ] && break
    [ "$status" = "unhealthy" ] && break
    sleep 2
done
if [ "$status" = "healthy" ]; then
    ok "app healthcheck = healthy"
else
    bad "app healthcheck = ${status:-unknown}（期望 healthy）"
    docker compose logs --tail=60 app || true
fi

step "5. 从宿主机看这一屏"
BASE="http://127.0.0.1:${PORT}"

code="$(curl -sS -o /tmp/acp-verify-root.html -w '%{http_code}' "$BASE/" || echo 000)"
if [ "$code" = "200" ] && grep -q 'id="root"' /tmp/acp-verify-root.html; then
    ok "GET / ⇒ 200 且是看板 HTML（含 #root 挂载点）"
else
    bad "GET / ⇒ ${code}（期望 200 + #root）"
fi

code="$(curl -sS -o /tmp/acp-verify-tasks.json -w '%{http_code}' "$BASE/tasks" || echo 000)"
if [ "$code" = "200" ] && grep -q '\[' /tmp/acp-verify-tasks.json; then
    ok "GET /tasks ⇒ 200 JSON（空库时是 []）"
else
    bad "GET /tasks ⇒ ${code}（期望 200 + JSON 数组；503 说明会话工厂没注进来）"
fi

code="$(curl -sS -o /tmp/acp-verify-approvals.json -w '%{http_code}' "$BASE/approvals" || echo 000)"
if [ "$code" = "200" ]; then
    ok "GET /approvals ⇒ 200（审批面接得上库）"
else
    bad "GET /approvals ⇒ ${code}（期望 200）"
fi

code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$BASE/" || echo 000)"
if [ "$code" = "400" ]; then
    ok "伪 Host（evil.example）⇒ 400（TrustedHostMiddleware 生效）"
else
    bad "伪 Host ⇒ ${code}（期望 400：Host 守卫没装上）"
fi

step "6. 库里真的建过表"
if docker compose exec -T app alembic current 2>/dev/null | grep -qE '^[0-9a-f]{6,}'; then
    ok "alembic current 有版本号 ⇒ 启动脚本跑过 upgrade head"
else
    bad "alembic current 读不到版本号（迁移没跑？）"
fi
if docker compose exec -T app python -c "
import os, psycopg
with psycopg.connect(os.environ['ACP_DB_DSN']) as c:
    n = c.execute(\"select count(*) from information_schema.tables where table_schema='public'\").fetchone()[0]
    assert n >= 10, n
print(n)
" >/tmp/acp-verify-tables.txt 2>/dev/null; then
    ok "public schema 有 $(cat /tmp/acp-verify-tables.txt) 张表"
else
    bad "表数量不足 10 ⇒ 迁移没落库"
fi

step "7. 运行形态"
uid="$(docker compose exec -T app id -u 2>/dev/null | tr -d '\r' || echo '?')"
if [ "$uid" = "10001" ]; then
    ok "容器以非 root（uid 10001）运行"
else
    bad "容器 uid = ${uid}（期望 10001）"
fi
published="$(docker compose port app 8765 2>/dev/null | tr -d '\r' | head -n1)"
case "$published" in
    127.0.0.1:*) ok "发布端口只在环回：${published}" ;;
    *) bad "发布端口是 ${published:-<空>}（期望 127.0.0.1:…；发到 0.0.0.0 等于把无鉴权写端点放上局域网）" ;;
esac
if docker compose logs app 2>/dev/null | grep -q 'alembic upgrade head'; then
    ok "启动日志里有 'alembic upgrade head'"
else
    bad "启动日志里没有迁移那一步"
fi

step "结果"
printf '  %s通过 %d 项 · 失败 %d 项%s\n' "$([ "$FAIL" -eq 0 ] && printf '%s' "$GREEN" || printf '%s' "$RED")" "$PASS" "$FAIL" "$OFF"

if [ "$FAIL" -eq 0 ]; then
    if [ "$KEEP" -eq 1 ]; then
        printf '\n全绿。看板开着：%s\n拆掉它：docker compose down -v\n' "$BASE"
    else
        printf '\n%s全绿，正在拆除。想自己点开看就重跑一次并加 --keep%s\n' "$DIM" "$OFF"
    fi
    cleanup
    exit 0
fi

printf '\n%s栈还在：docker compose logs -f app 看日志，docker compose down -v 拆掉%s\n' "$DIM" "$OFF"
exit 1
