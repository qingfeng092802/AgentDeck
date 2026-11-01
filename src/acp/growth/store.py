"""个人进化系统的档案载体（owner 2026-10-19 MVP-2 第三步：事实库 + 错题本）。

这个包只管**档案本身**：它住在哪儿、里面是什么、怎么原样进出去。它不知道有没有 HTTP，
也不知道有哪支 transport —— 注入那一头在 `acp.adapters.*`，读端面在 `acp.api.growth`。

三条口径，都在 `tests/guards/test_growth_store.py` 里各钉了一条：

1. **载体位置只有一个事实源** = `config/params-ref.md §11` 的 ```text 围栏。环境变量名与默认目录名
   都在那里成文，代码读的是解析结果与自己的常量，两侧的绑定是**行为式**的（把围栏里那个变量名
   写进环境，根目录必须跟着走）—— 口径同 §10 与 `archive/root.py`。
2. **全文、无条件、不截断**。`render_block()` 不做摘要、不做长度上限、不按"相关性"筛：
   owner 那句"确保不丢字"在这里是可证伪的（一条超长档案必须整段出现在输出里）。
3. **拒写而不是改写**。保存前过一遍 `config/loader.redaction_hits()`，命中凭据形态就抛错。
   静默把用户的字洗成 `[REDACTED]` 会同时违背"不丢字"与"凭据不落文件"两条 ——
   前者被偷偷破坏，而后者看起来被遵守了。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from acp.config.loader import REPO_ROOT, redaction_hits

#: `§11` 围栏里的环境变量名。改它要同时改那一节，否则"唯一事实源"就断在代码这一头。
GROWTH_ROOT_ENV = "ACP_GROWTH_ROOT"

#: 默认目录名（相对仓库根）。
DEFAULT_ROOT_NAME = "growth"

#: 两份档案的文件名。**封闭集合**：不接受调用方给别的名字，否则"读档案的接口"就变成
#: "读仓库里任意一个文件的接口"，而那个面一旦开出来就再也关不上。
PROFILE_FILE = "my_profile.md"
MISTAKES_FILE = "mistakes.md"
ARCHIVES: tuple[str, ...] = (PROFILE_FILE, MISTAKES_FILE)

#: 注入块的两段标题。判据与 transport 都按这两行定位，改名要三处同笔。
PROFILE_HEADER = "【事实档案·全文注入·不截断】"
MISTAKES_HEADER = "【错题本·全文注入·不截断】"

#: 单份档案的大小上界。它**不是**"注入时截到这么多"，而是"超过就不许保存"：
#: 与其在报文里悄悄丢字，不如在写的时候就拒绝，让"太长"这件事有一个看得见的失败。
MAX_ARCHIVE_BYTES = 256 * 1024


class GrowthError(ValueError):
    """档案面用错了（名字不在封闭集合、文件不存在、内容过界或含凭据形态）。"""


def growth_root(
    root: Path | str | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """解析根目录：显式参数 > `ACP_GROWTH_ROOT` > 仓库根下的默认目录名。

    空白值按"没设"处理（口径同 `archive/root.py::resolve_bus_root()`）：`ACP_GROWTH_ROOT=""`
    如果照字面用，根就变成当前工作目录本身，档案会跟整个仓库混在一起。
    """
    if root is not None:
        return Path(root)
    source = os.environ if env is None else env
    declared = (source.get(GROWTH_ROOT_ENV) or "").strip()
    if not declared:
        return REPO_ROOT / DEFAULT_ROOT_NAME
    candidate = Path(declared)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def path_for(name: str, *, root: Path | str | None = None) -> Path:
    if name not in ARCHIVES:
        raise GrowthError(
            f"档案名只接受 {list(ARCHIVES)}（收到 {name!r}）⇒ 别的名字等于把读接口开成读仓库任意文件"
        )
    return growth_root(root) / name


def read(name: str, *, root: Path | str | None = None) -> str:
    """原样读出全文。**文件不存在 ⇒ 抛**，不返回空串。

    空串会把"档案还没落地"读成"档案是空的"，而这两件事在看板上长得不一样：
    前者要提示先建档案，后者是正常状态。仓库里这两份模板是随本笔落地的，所以生产路径上
    报缺就意味着被人删了或根指错了。
    """
    target = path_for(name, root=root)
    try:
        # `utf-8-sig`：使用者可能用 PowerShell / 记事本存过，BOM 不是内容（口径同 danger_list）。
        return target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GrowthError(
            f"档案读不出来：{target}（reason={type(exc).__name__}）"
            "⇒ 不返回空串，因为'没落地'与'空档案'是两件事"
        ) from None


def digest_of(text: str) -> str:
    """正文指纹的唯一算式（`sha256:` + 解码后正文的十六进制）。

    读回、写回、判据三处都用这一支 —— 两处各写一遍哈希口径，就会出现"同一份内容两个指纹"，
    而那个 409 覆盖保护正是靠指纹相等来判的。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of(name: str, *, root: Path | str | None = None) -> str:
    """内容指纹（解码后正文的哈希）。看板拿它做"我没看过新版本就别覆盖"的凭据。"""
    return digest_of(read(name, root=root))


def write(name: str, text: str, *, root: Path | str | None = None) -> str:
    """原子写入全文，回写后的指纹。**要么整段存下，要么一格都不动**。

    两道拒都在落盘之前：过界（`MAX_ARCHIVE_BYTES`）与含凭据形态。
    写入走"同目录临时文件 + `os.replace`"，为的是半途失败不许留下半份档案 ——
    档案被截成一半比写失败更坏：下半段是上一次的旧内容，读的人看不出来。
    """
    target = path_for(name, root=root)
    payload = text.encode("utf-8")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise GrowthError(
            f"{name} 有 {len(payload)} 字节，超出上界 {MAX_ARCHIVE_BYTES} ⇒ 拒写。"
            "这里没有'注入时截断'那条路：丢字发生在报文里就没人能发现"
        )
    if redaction_hits(text):
        raise GrowthError(
            f"{name} 里出现凭据形态 ⇒ 拒写（宪法「二」：凭据不落文件）。"
            "刻意不做'洗掉再存'：那既改了你的字，又把'凭据已经进了档案'这件事抹平"
        )
    if not target.parent.is_dir():
        raise GrowthError(f"档案根不存在：{target.parent}（先建目录，或检查 {GROWTH_ROOT_ENV}）")
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return digest_of(text)


def render_block(*, root: Path | str | None = None) -> str:
    """两份档案的**全文**拼成一段前缀，供每次外部执行进场。

    空文件也发标题行 ⇒ "无条件"这件事是可见的：判据能在档案为空时断言"标题仍在"，
    而不是只在有内容时断言"内容在场"（后者会允许"有内容才注入"这一条偷偷生效的实现）。
    """
    parts = []
    for header, name in ((PROFILE_HEADER, PROFILE_FILE), (MISTAKES_HEADER, MISTAKES_FILE)):
        parts.append(f"{header}\n{read(name, root=root)}")
    return "\n\n".join(parts) + "\n\n"
