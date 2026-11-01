"""P0-5：一键启动的三个文件必须互相咬合，且不许把凭据烘进镜像。

为什么用判据而不是靠"跑一遍 docker"：开发机上没有 docker CLI（本机实测），而这三个文件
一旦漂开，症状是**别人**克隆之后起不来 —— 最贵的那种回归。所以把"它们必须一致"写成断言：

- `Dockerfile` 两段、段一真的 `npm ci && npm run build`、段二真的从段一取 `dist`；
- 启动脚本先迁移后起服务，缺 DSN 就在迁移前退出；
- `docker-compose.yml` 的 app 用 `build: .`、等 db healthy、`env_file: .env`，且**容器内**
  的 DSN 指向服务名 `db:5432`（不是 .env 里那串宿主机口径）；
- 发布端口仍在环回（安全边界靠这一行守），容器内才允许绑 `0.0.0.0`；
- `.dockerignore` 挡住 `.env` 与 `node_modules`。

真跑起来这件事交给 `scripts/verify-docker.sh`（需要一台有 Docker 的机器）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify-docker.sh"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ignore() -> str:
    return DOCKERIGNORE.read_text(encoding="utf-8")


# ------------------------------------------------------------------ Dockerfile


def test_the_image_is_built_in_two_stages_and_the_first_one_builds_the_board(dockerfile: str) -> None:
    stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\w+)", dockerfile, flags=re.MULTILINE)
    assert stages == ["web", "runtime"], f"段划分变了：{stages}"

    web_stage = dockerfile.split("AS web", 1)[1].split("AS runtime", 1)[0]
    assert "npm ci" in web_stage, "段一没装依赖"
    assert "npm run build" in web_stage, "段一没出产物（build = tsc --noEmit && vite build）"


def test_the_runtime_stage_takes_the_board_from_the_first_stage(dockerfile: str) -> None:
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert re.search(r"^COPY\s+--from=web\s+/web/dist\s+\./web/dist$", runtime, flags=re.MULTILINE), (
        "运行段没有从段一取 dist ⇒ 镜像里没有看板"
    )


def test_dependencies_come_from_pyproject_as_an_editable_install(dockerfile: str) -> None:
    """editable 不是口味问题：非 editable 会让 `REPO_ROOT` 指到 site-packages 那一层。

    判据同时守住"不许另抄一份 requirements.txt"——两份依赖清单必然漂开。
    """
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert re.search(r"pip install[^\n]*-e\s+\.", runtime), "依赖装法不是 editable"
    assert "COPY requirements" not in dockerfile, "镜像里另抄了一份 requirements"
    assert not (REPO_ROOT / "requirements.txt").exists(), "仓库里出现了第二份依赖清单"
    assert "COPY pyproject.toml" in runtime


def test_the_container_is_told_it_may_bind_all_interfaces(dockerfile: str) -> None:
    """没有这一项，容器里的 `resolve_host` 会拒掉 0.0.0.0，端口映射永远打不进来。"""
    assert "ACP_ALLOW_NON_LOOPBACK=1" in dockerfile


def test_the_image_runs_as_a_non_root_user(dockerfile: str) -> None:
    assert re.search(r"^USER\s+(?!root)\w+", dockerfile, flags=re.MULTILINE), "没有降权到非 root"


# ------------------------------------------------------------------ 启动脚本


def test_migrations_run_before_the_server(entrypoint: str) -> None:
    """先迁移后起服务，且是同一段序列 —— 顺序反了，第一屏就是 500（表还不存在）。"""
    assert "alembic upgrade head" in entrypoint
    assert entrypoint.index("alembic upgrade head") < entrypoint.index("-m acp.serve"), (
        "迁移没有排在起服务之前"
    )
    assert entrypoint.rstrip().endswith('"$@"') or 'exec python -m acp.serve' in entrypoint


def test_the_script_refuses_to_start_without_a_dsn(entrypoint: str) -> None:
    """DSN 缺失必须在等库那一步就退出，而不是让 alembic 报一句看不懂的驱动错误。"""
    assert "ACP_DB_DSN" in entrypoint
    assert "raise SystemExit" in entrypoint
    assert "postgresql://" not in entrypoint, "启动脚本里出现了 DSN 字面量 ⇒ 凭据可能被写死"


def test_the_script_passes_the_board_dir_explicitly(entrypoint: str) -> None:
    """`--dist` 必须显式给：默认值是 `REPO_ROOT/web/dist`，容器里靠 REPO_ROOT 推容易踩空。"""
    assert "--dist /app/web/dist" in entrypoint


# ------------------------------------------------------------------ compose


def test_compose_builds_the_app_and_waits_for_a_healthy_database(compose: dict) -> None:
    app = compose["services"]["app"]
    assert app["build"]["context"] == ".", "app 不是从本仓库构建的"
    assert app["depends_on"]["db"]["condition"] == "service_healthy", "没等数据库健康就起"
    assert "healthcheck" in compose["services"]["db"], "db 没有 healthcheck ⇒ service_healthy 永远满足不了"
    assert "healthcheck" in app, "app 自己没有 healthcheck ⇒ `docker compose ps` 看不出它到底活没活"


def test_compose_injects_env_from_the_env_file(compose: dict) -> None:
    app = compose["services"]["app"]
    assert app["env_file"] == [".env"], app.get("env_file")


def test_the_dsn_inside_the_container_points_at_the_service_name(compose: dict) -> None:
    """`.env` 里那串是宿主机口径（127.0.0.1:5434）；容器里必须换成 db:5432。

    这一条最容易错：照抄 `.env` 的 DSN，容器里的 127.0.0.1 指它自己，迁移会一直
    `connection refused`，而错误信息看起来像"库没起"。
    """
    dsn = compose["services"]["app"]["environment"]["ACP_DB_DSN"]
    assert "@db:5432/acp" in dsn, dsn
    assert "127.0.0.1" not in dsn and "localhost" not in dsn, dsn
    assert ":?" in dsn, "口令没有走 `${ACP_PG_PASSWORD:?}` ⇒ 缺变量时会悄悄起一个空口令"


def test_the_published_port_stays_on_loopback(compose: dict) -> None:
    """容器内绑 0.0.0.0（否则打不进来），发布侧仍只发到宿主环回 —— 这一行是安全边界。"""
    ports = compose["services"]["app"]["ports"]
    assert len(ports) == 1, ports
    assert str(ports[0]).startswith("127.0.0.1:"), ports[0]
    assert compose["services"]["app"]["environment"]["ACP_ALLOW_NON_LOOPBACK"] == "1"


# ------------------------------------------------------------------ 上下文与验证脚本


def test_the_build_context_never_carries_credentials_or_node_modules(ignore: str) -> None:
    lines = {line.strip() for line in ignore.splitlines() if line.strip() and not line.startswith("#")}
    assert ".env" in lines, "`.env` 没被排除 ⇒ 口令会进镜像层"
    assert "**/node_modules" in lines or "node_modules" in lines
    assert "!.env.example" in lines, "把 .env.example 也排掉会让人以为没有样例可抄"


def test_the_verify_script_checks_the_things_that_matter() -> None:
    """验证脚本是"没有 docker 的机器"上唯一的验收路径，它断的项要覆盖安全与功能两面。"""
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")
    for needle in (
        "alembic current",
        "evil.example",
        "docker compose port app 8765",
        "id=\"root\"",
        "/app/.env",
        "/app/web/dist/index.html",
    ):
        assert needle in text, f"验证脚本没断 {needle!r}"
