"""一次性回填：已有交付包但需求单仍是 SPLIT；并把旧 manifest 的字节快照对齐磁盘。

用法（默认 dry-run，只打印；确认后加 --apply 落库）::

    PYTHONPATH=src .venv/Scripts/python.exe scripts/backfill_legacy_delivery.py --apply

两类回填：

1. `feature_request.status == 'SPLIT'` 且存在 `deliverable` 行 ⇒ 走 `run.mark_request_delivered()`
   写 `DELIVERED`（同一处唯一写者，留 EN-14 一行）。
2. 每条 manifest 逐项 `stat()`：文件在盘上 ⇒ 用实测字节/行数修正旧快照；文件缺失 ⇒
   在 `deliverable.open_issues` 里登记 `artifact_stale`（不删项，避免"少几条却仍成功"）。
   修正后再跑一遍 `assemble._assert_manifest_matches_disk()`；仍不过的登记到 issues，不静默跳过。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

from acp.archive import root as archive
from acp.orchestrate.nodes.assemble import (
    AssembleMisuse,
    ManifestEntry,
    _assert_manifest_matches_disk,
)
from acp.persistence import session as session_module
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import Deliverable
from acp import run as run_module


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _entry_of(item: dict) -> ManifestEntry:
    return ManifestEntry(
        rel_path=str(item.get("rel_path") or ""),
        producer_agent_id=str(item.get("producer") or ""),
        task_card_id=str(item.get("task_card_id") or ""),
        bytes=int(item.get("bytes") or 0),
        lines=int(item.get("lines") or 0),
        format=str(item.get("format") or ""),
    )


def _issue_exists(issues: list[object], rel_path: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("code") == "artifact_stale" and item.get("rel_path") == rel_path
        for item in issues
    )


def backfill(*, bus_root: pathlib.Path, apply: bool) -> dict[str, int]:
    _load_env()
    factory = session_module.make_session_factory()
    session = factory()
    counts = {
        "packages": 0,
        "members": 0,
        "bytes_fixed": 0,
        "missing": 0,
        "issues_added": 0,
        "requests_delivered": 0,
        "still_stale": 0,
    }
    now = dt.datetime.now(dt.timezone.utc)
    try:
        packages = list(session.query(Deliverable).order_by(Deliverable.created_at).all())
        counts["packages"] = len(packages)
        for package in packages:
            request = session.get(FeatureRequest, package.feature_request_id)
            if request is None:
                continue
            prior_status = str(request.status)
            project_root = archive.archive_root(str(request.project_key), bus_root=bus_root)
            manifest = [dict(item) for item in (package.manifest or []) if isinstance(item, dict)]
            issues = [item for item in (package.open_issues or []) if isinstance(item, dict)]
            counts["members"] += len(manifest)
            changed = False
            for item in manifest:
                rel_path = str(item.get("rel_path") or "")
                target = project_root / rel_path
                if not target.is_file():
                    counts["missing"] += 1
                    if not _issue_exists(issues, rel_path):
                        issues.append(
                            {
                                "code": "artifact_stale",
                                "rel_path": rel_path,
                                "reason": "文件在盘上不存在，旧 manifest 的字节快照无法核验",
                            }
                        )
                        counts["issues_added"] += 1
                    continue
                actual_bytes = target.stat().st_size
                if int(item.get("bytes") or 0) != actual_bytes:
                    item["bytes"] = actual_bytes
                    counts["bytes_fixed"] += 1
                    changed = True
                try:
                    actual_lines = len(target.read_bytes().decode("utf-8").splitlines())
                except UnicodeDecodeError:
                    actual_lines = None
                if actual_lines is not None and int(item.get("lines") or 0) != actual_lines:
                    item["lines"] = actual_lines
                    changed = True
            if changed:
                package.manifest = manifest
            if issues:
                package.open_issues = issues
            try:
                _assert_manifest_matches_disk([_entry_of(item) for item in manifest], project_root)
            except AssembleMisuse as exc:
                counts["still_stale"] += 1
                if not _issue_exists(issues, "<manifest>"):
                    issues.append(
                        {
                            "code": "artifact_stale",
                            "rel_path": "<manifest>",
                            "reason": str(exc)[:500],
                        }
                    )
                    package.open_issues = issues
            if str(request.status) == "SPLIT":
                result = run_module.mark_request_delivered(
                    session,
                    request.id,
                    now=now,
                    deliverable_id=package.id,
                )
                if result == run_module.REQUEST_DELIVERED:
                    counts["requests_delivered"] += 1
            print(
                f"[backfill] {request.project_key} · {package.id} · members={len(manifest)} "
                f"· status_before={prior_status}"
            )
        if apply:
            session.commit()
            print("[backfill] COMMITTED")
        else:
            session.rollback()
            print("[backfill] DRY-RUN（未写库；加 --apply 落库）")
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 SPLIT 需求单与旧交付 manifest")
    parser.add_argument("--bus-root", type=pathlib.Path, default=ROOT / "results")
    parser.add_argument("--apply", action="store_true", help="真正提交；缺省只打印（dry-run）")
    args = parser.parse_args(argv)
    counts = backfill(bus_root=args.bus_root.resolve(), apply=args.apply)
    print("[backfill] " + " ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
