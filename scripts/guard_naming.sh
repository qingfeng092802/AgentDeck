#!/usr/bin/env bash
# 宪法「二」防回归护栏（已否决命名）的可执行入口。
# 唯一事实源 = config/params-ref.md §7 的 fenced block 里**第一个 grep 语句**
# （自 `grep -rniE` 起、到不以 `\` 续行为止）：本脚本从那里解析 --include / --exclude /
# --exclude-dir，不在别处复制第二份清单。
# 只解析这一条语句是刻意的：§7 的正文里留着"原写 --exclude='tasks-review-*.md'"这类**历史留痕**，
# 若按整节解析，那些已被裁决废弃的通配排除项会被重新当成活参数吃进来（2026-10-11 实测踩到）。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$REPO_ROOT/config/params-ref.md"

die() { echo "GUARD ERROR :: $*" >&2; exit 2; }

[ -f "$SPEC" ] || die "缺少 $SPEC（§7 清单的唯一事实源）"

# 第一个 grep 语句（含续行）
STATEMENT="$(awk '
  /^## 7\. /{in7=1; next}
  /^## 8\./{exit}
  in7 && /^grep -rniE/{grab=1}
  grab{print; if ($0 !~ /\\$/) exit}
' "$SPEC")"
[ -n "$STATEMENT" ] || die "§7 里找不到以 'grep -rniE' 起首的门禁语句"

BAN="$(awk '/^## 7\. /{in7=1; next} /^## 8\./{exit} in7 && /^BAN=/{sub(/^BAN='\''/, ""); sub(/'\''$/, ""); print; exit}' "$SPEC")"
[ -n "$BAN" ] || die "§7 未找到 BAN='…' 赋值行"

tokens() { printf '%s\n' "$STATEMENT" | grep -oE -- "--$1=[^ ]+" | sed "s/^--$1=//; s/'//g"; }

readarray -t INCLUDES  < <(tokens include)
readarray -t EXCLUDES  < <(tokens exclude)
readarray -t EXCLDIRS  < <(tokens exclude-dir)

[ "${#INCLUDES[@]}" -gt 0 ] || die "§7 门禁语句里没有 --include= 扫描面"
[ "${#EXCLUDES[@]}" -gt 0 ] || die "§7 门禁语句里没有 --exclude= 登记项"

# §7 的清单本身必然逐字写着禁用项；它不在排除表里时整条门禁会自指导火。
printf '%s\n' "${EXCLUDES[@]}" | grep -qx 'params-ref.md' \
  || die "排除表里没有 params-ref.md ⇒ 门禁会命中清单自身，登记已破口"
# 通配排除项 = 未登记的未来文件自动豁免，本表的收紧裁决明令禁止。
for t in "${EXCLUDES[@]}"; do
  case "$t" in *'*'*) die "排除项 $t 含通配 ⇒ 违反「排除的是经登记的具体文件名」" ;; esac
done

INC=(); for t in "${INCLUDES[@]}"; do INC+=(--include="$t"); done
EXC=(); for t in "${EXCLUDES[@]}"; do EXC+=(--exclude="$t"); done
EXD=(); for t in "${EXCLDIRS[@]}"; do EXD+=(--exclude-dir="$t"); done

if [ "${1:-}" = "--show-spec" ]; then
  echo "BAN-source=$SPEC (section 7, first grep statement)"
  printf 'include=%s\n' "${INCLUDES[@]}"
  printf 'exclude=%s\n' "${EXCLUDES[@]}"
  printf 'exclude-dir=%s\n' "${EXCLDIRS[@]}"
  exit 0
fi

if [ "${1:-}" = "--positive-control" ]; then
  # 正对照：证明扫描管线（扫描面 / 排除表 / 退出码）真的会开火，而不是恒绿。
  # 用无害词，避免为了自证而把被否决命名再抄一遍。
  PROBE="${2:-}"
  [ -n "$PROBE" ] || die "--positive-control 需要参数"
  HITS="$(grep -rniE "$PROBE" "${INC[@]}" "${EXC[@]}" "${EXD[@]}" "$REPO_ROOT" | wc -l | tr -d ' ')"
  echo "POSITIVE-CONTROL pattern=$PROBE hits=$HITS"
  [ "$HITS" -gt 0 ] || die "正对照零命中 ⇒ 扫描面为空或被排除表吞掉，门禁是死判据"
  echo "SUMMARY positive-control-fired=True"
  exit 0
fi

[ $# -eq 0 ] || die "未知参数：$*（可用：无参 / --show-spec / --positive-control <无害词>）"

# 主扫描：我方产物里出现被否决命名即为护栏被破坏。
MAIN_HITS="$(grep -rniE "$BAN" "${INC[@]}" "${EXC[@]}" "${EXD[@]}" "$REPO_ROOT" | wc -l | tr -d ' ')"

# .env 系单独扫：整行可能带凭据值，-o 只回显命中的 token，避免把行抄进终端 / CI 日志。
ENV_HITS="$(grep -rnoiE "$BAN" --include='.env*' "${EXD[@]}" "$REPO_ROOT" 2>/dev/null | wc -l | tr -d ' ')"

echo "naming-guard main=$MAIN_HITS env=$ENV_HITS"
if [ "$MAIN_HITS" -eq 0 ] && [ "$ENV_HITS" -eq 0 ]; then
  echo "SUMMARY main=0 env=0 gate=PASS"
else
  echo "SUMMARY main=$MAIN_HITS env=$ENV_HITS gate=FAIL"
fi
[ "$MAIN_HITS" -eq 0 ] && [ "$ENV_HITS" -eq 0 ]
