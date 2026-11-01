# syntax=docker/dockerfile:1
#
# AgentDeck 一键启动镜像（P0-5）。两段构建：第一段出看板产物，第二段拼运行时。
#
# 为什么运行段用 `pip install -e .` 而不是 `pip install .`：
# `acp.config.loader.REPO_ROOT = Path(__file__).resolve().parents[3]`，装进 site-packages
# 之后它指向的是 `…/python3.12`（而不是仓库根），于是 `config/danger_list.yaml`、
# `web/dist`、`results/` 三处全部指错位置 —— 表现是服务起得来、看板 404、危险清单读不到。
# editable 安装让 `acp` 仍然从 `/app/src` 导入，REPO_ROOT 才是 `/app`，与源码树里跑测试
# 是同一套拼法。依赖仍然只从 `pyproject.toml` 来，不另抄一份 requirements.txt（两处必漂开）。

# ---------------------------------------------------------------- 段一：看板产物
#
# 基镜像做成 ARG（2026-10-31）：本机与 WSL 侧都 `docker manifest inspect` 不到 Docker Hub 的
# `node:20-alpine` / `python:3.12-slim`，而"发布闸全绿才算过"不能建立在一个拉不到的默认值上。
# 默认值保持官方名（有网机器行为不变）；换源/用本地镜像走 `verify-docker.sh --py-base/--node-base`
# 或 `docker compose build --build-arg`。镜像里的四条事实（非 root、无 .env、dist 烘进去、
# 迁移目录在）与基镜像取哪一份无关，所以换源不改变验收口径。
ARG NODE_BASE=node:20-alpine
ARG PY_BASE=python:3.12-slim

FROM ${NODE_BASE} AS web

WORKDIR /web

# 先只拷清单装依赖：`web/src` 改一行不该让 `npm ci` 重跑一遍。
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
# `npm run build` = `tsc --noEmit && vite build`：类型不过就不出产物，不留半成品 dist。
RUN npm run build


# ---------------------------------------------------------------- 段二：运行时
FROM ${PY_BASE} AS runtime

# `ACP_ALLOW_NON_LOOPBACK=1` 是容器口径必需项：容器里绑 127.0.0.1 只指容器自己，
# `-p 127.0.0.1:8765:8765` 也打不进来。安全边界由发布侧守 —— docker-compose.yml 只把
# 端口发到宿主环回；`serve.resolve_host()` 的默认值仍然是"只许环回"。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ACP_ALLOW_NON_LOOPBACK=1

WORKDIR /app

# editable 装的是"路径"，所以依赖与源码同步落地：改代码就重装，换来的是镜像里只有一份源码。
# 其余目录（config/、alembic.ini、web/dist）在下面才拷进来 —— 它们不参与依赖解析。
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

COPY config/ ./config/
COPY alembic.ini ./
COPY docker/entrypoint.sh /usr/local/bin/acp-entrypoint
COPY --from=web /web/dist ./web/dist

# 非 root 起服务；总线根与进化档案根预建出来，挂卷后属主才对。
RUN chmod +x /usr/local/bin/acp-entrypoint \
    && useradd --create-home --uid 10001 acp \
    && mkdir -p /app/results /app/growth \
    && chown -R acp:acp /app

USER acp

EXPOSE 8765

# 启动脚本自己打日志、等库、跑迁移，所以这里不写 CMD；
# `docker compose run --rm app --api-only` 之类追加的参数会原样透传给 acp.serve。
ENTRYPOINT ["/usr/local/bin/acp-entrypoint"]
