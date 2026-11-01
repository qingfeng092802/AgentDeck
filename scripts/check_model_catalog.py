"""模型目录漂移检查：`web/src/lib/modelCatalog.ts` 里的型号名，账号里到底叫不叫得通。

    python scripts/check_model_catalog.py            # 三家都查（缺 key 的那家报「未核」）

为什么要有这一支：2026-10-30 之前这份目录是**照产品叙事写的**，不是照厂商返回写的 ——
豆包 8 个名字里 7 个在账号里 404（`doubao-seed-2.1-lite` 的真名是 `doubao-seed-2-1-lite-260915`），
DeepSeek 6 个里只有 2 个可用。设置页选一个 404 的名字，四席直跑会在规划之前死掉，
而报出来的话是"型号不可用"，看不出错的是我们自己那一行名单。

密钥只从环境变量读，任何输出都不打印取值：
`ACP_DEEPSEEK_KEY` / `ACP_QWEN_KEY` / `ACP_DOUBAO_KEY`（豆包也接受仓里既有的 `ARK_API_KEY`）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "web" / "src" / "lib" / "modelCatalog.ts"

#: 厂商 id → (显示名, base_url, 密钥环境变量候选名单，按顺序取第一个有值的)
VENDORS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "deepseek": ("深度求索 DeepSeek", "https://api.deepseek.com/v1", ("ACP_DEEPSEEK_KEY",)),
    "qwen": ("阿里云通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", ("ACP_QWEN_KEY",)),
    "doubao": ("字节跳动豆包", "https://ark.cn-beijing.volces.com/api/v3", ("ACP_DOUBAO_KEY", "ARK_API_KEY")),
}


def catalog_models() -> dict[str, list[str]]:
    """从 TS 里读三家各自的 `models: [...]`。读的是源码，不 import 构建产物。

    先把 `//` 注释行整行摘掉再抽字符串：名单里那句解释一旦带引号（"用千问的 key 走…"），
    按引号抽就会多出一个假型号 —— 这一发是这支脚本自己撞出来的。
    """
    raw = CATALOG.read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in raw if not line.lstrip().startswith("//"))
    found: dict[str, list[str]] = {}
    for vendor_id in VENDORS:
        block = re.search(rf'id:\s*"{vendor_id}"[\s\S]*?models:\s*\[([\s\S]*?)\]', text)
        if block is None:
            raise SystemExit(f"目录里找不到 {vendor_id} 的 models 名单 ⇒ 载体形状变了，先改这里")
        found[vendor_id] = re.findall(r'"([^"]+)"', block.group(1))
    return found


def live_models(base_url: str, key: str) -> list[str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"读 /models 失败 {base_url} ⇒ HTTP {exc.code}：{exc.read(200).decode('utf-8', 'replace')}") from None
    except BaseException as exc:  # noqa: BLE001
        raise SystemExit(f"读 /models 失败 {base_url} ⇒ {type(exc).__name__}") from None
    items = body.get("data") or body.get("result") or []
    return sorted({str(item.get("id") or item.get("name")) for item in items if isinstance(item, dict)})


def main() -> int:
    # 本机控制台默认按 cp936 落盘，消息里的 `⇒` 会直接把这一发打崩（同 `scripts/check_tables.py` 的处理）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    drift = 0
    listed_by_vendor = catalog_models()
    for vendor_id, (display, base_url, env_names) in VENDORS.items():
        key = next((os.environ[name].strip() for name in env_names if os.environ.get(name, "").strip()), "")
        listed = listed_by_vendor[vendor_id]
        if not key:
            print(f"{display} ⇒ 未核（缺 {'/'.join(env_names)}）；目录里 {len(listed)} 个名字没有账号可比对")
            continue
        live = set(live_models(base_url, key))
        dead = [name for name in listed if name not in live]
        print(f"{display} ⇒ 目录 {len(listed)} 个｜账号 {len(live)} 个｜叫不通 {len(dead)} 个")
        for name in dead:
            print(f"   漂移：{name}")
            drift += 1
    if drift:
        print(f"共 {drift} 个型号名在账号里叫不通 ⇒ 目录要重做")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
