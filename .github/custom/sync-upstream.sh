#!/usr/bin/env bash
# 在 GitHub Actions 里把上游 rustdesk 摊平合并进本仓库，并重新套用全部定制。
# 相当于 sync.sh 的 CI 版，但不依赖任何本地环境。
#
# 用法: sync-upstream.sh <upstream_tag|commit>
# 退出码: 0 = 已同步   2 = 定制无法套用（拒绝提交）   1 = 其他错误
set -euo pipefail

TAG="${1:?usage: sync-upstream.sh <upstream_tag|commit>}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- 定制参数 ----
RENDEZVOUS="dk6.ztsc.host"
ADMIN="http://dk6.ztsc.host:21114"
PUBKEY="XuICuhdJOMB8yFj85s7qwXnRfokuAjG6Bojw1ggLQuo="

# 默认走 HTTPS（GitHub Actions 上直接可用）；受限网络下可用环境变量覆盖为 SSH。
RUSTDESK_URL="${UPSTREAM_RUSTDESK_URL:-https://github.com/rustdesk/rustdesk.git}"
HBB_URL="${UPSTREAM_HBB_URL:-https://github.com/rustdesk/hbb_common.git}"

CUSTOM_DIR=".github/custom"      # 本仓库自有内容，同步后需保留
KEEP_WORKFLOWS="auto-release.yml auto-sync.yml publish-update-manifest.yml"

log() { printf '\n\033[1;32m[sync]\033[0m %s\n' "$*"; }
err() { printf '\n\033[1;31m[sync:error]\033[0m %s\n' "$*"; }
die() { err "$*"; exit 1; }

log "目标上游版本: $TAG"

# ---------- 1. 取上游源码 ----------
git init --quiet "$WORK/rustdesk" || die "初始化临时仓库失败"
git -C "$WORK/rustdesk" fetch --quiet --depth=1 "$RUSTDESK_URL" "$TAG" \
  || die "拉取 rustdesk@$TAG 失败"
git -C "$WORK/rustdesk" checkout --quiet FETCH_HEAD || die "检出 rustdesk@$TAG 失败"

HB_COMMIT="$(git -C "$WORK/rustdesk" ls-tree HEAD libs/hbb_common | awk '{print $3}')"
[ -n "$HB_COMMIT" ] || die "无法从上游 submodule 解析 hbb_common 提交"
log "hbb_common 提交: $HB_COMMIT"

git clone --quiet "$HBB_URL" "$WORK/hbb_common" || die "克隆 hbb_common 失败"
git -C "$WORK/hbb_common" checkout --quiet "$HB_COMMIT" \
  || die "切换 hbb_common@$HB_COMMIT 失败"

# ---------- 2. 备份本仓库自有内容 ----------
mkdir -p "$WORK/keep/wf"
if [ -d "$CUSTOM_DIR" ]; then
  cp -a "$CUSTOM_DIR" "$WORK/keep/custom"
fi
for wf in $KEEP_WORKFLOWS; do
  [ -f ".github/workflows/$wf" ] && cp -a ".github/workflows/$wf" "$WORK/keep/wf/"
done

# ---------- 3. 用上游内容替换工作区（保留 .git） ----------
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
tar -C "$WORK/rustdesk" --exclude=.git -cf - . | tar -xf -

# ---------- 4. 摊平 hbb_common 到 libs/ ----------
rm -rf libs/hbb_common
mkdir -p libs/hbb_common
tar -C "$WORK/hbb_common" --exclude=.git -cf - . | tar -xf - -C libs/hbb_common
rm -f .gitmodules
printf "\n" > .gitignore

# ---------- 5. 还原自有内容 ----------
mkdir -p .github/workflows
[ -d "$WORK/keep/custom" ] && cp -a "$WORK/keep/custom" .github/
cp -a "$WORK/keep/wf/." .github/workflows/ 2>/dev/null || true

# ---------- 6. 定制：服务器地址 / 公钥 / 管理后台 ----------
CFG="libs/hbb_common/src/config.rs"
[ -f "$CFG" ] || die "找不到 $CFG"

sed -i "s/rs-ny\.rustdesk\.com/${RENDEZVOUS}/g" "$CFG"
sed -i "s|pub const RS_PUB_KEY: &str = \"[^\"]*\";|pub const RS_PUB_KEY: \&str = \"${PUBKEY}\";|g" "$CFG"

sed -i "s|https://admin.rustdesk.com|${ADMIN}|g" src/common.rs

# ---------- 7. 定制：隐藏服务器设置入口 ----------
DART="flutter/lib/desktop/pages/connection_page.dart"
[ -f "$DART" ] || die "找不到 $DART"

NEW_CODE='    Widget setupServerWidget() => Flexible(
       child: Offstage(
         offstage: !(!_svcStopped.value &&
             stateGlobal.svcStatus.value == SvcStatus.ready &&
             _svcIsUsingPublicServer.value),
         child: Row(
           crossAxisAlignment: CrossAxisAlignment.center,
           children: [], 
         ),
       ),
     );'
perl -i -pe '
    BEGIN { $/ = undef; $new_code = shift @ARGV; }
    s/\QsetupServerWidget() => Flexible(\E.*?\Q);\E/$new_code/ms;
' "$NEW_CODE" "$DART" || die "替换 setupServerWidget 失败"

# ---------- 8. 应用补丁 ----------
shopt -s nullglob
PATCHES=("$CUSTOM_DIR"/patches/*.patch)
shopt -u nullglob
for p in "${PATCHES[@]}"; do
  log "应用补丁: $(basename "$p")"
  if ! git apply --check "$p" 2>/dev/null; then
    err "补丁无法应用: $(basename "$p")"
    err "上游 $TAG 的代码结构可能已变化，需要重新生成该补丁。"
    exit 2
  fi
  git apply "$p" || { err "应用补丁失败: $(basename "$p")"; exit 2; }
done

# ---------- 9. 断言：定制必须全部生效 ----------
log "校验定制结果..."
fail=0
ok()  { printf '  \033[1;32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[1;31m✘\033[0m %s\n' "$1"; fail=1; }

grep -qF "RENDEZVOUS_SERVERS: &[&str] = &[\"${RENDEZVOUS}\"]" "$CFG" \
  && ok "RENDEZVOUS_SERVERS -> ${RENDEZVOUS}" || bad "RENDEZVOUS_SERVERS 未替换"
grep -qF "$PUBKEY" "$CFG" \
  && ok "RS_PUB_KEY 已替换" || bad "RS_PUB_KEY 未替换"
grep -q 'rs-ny\.rustdesk\.com' "$CFG" \
  && bad "仍有 rs-ny.rustdesk.com 残留" || ok "无 rs-ny.rustdesk.com 残留"
grep -qF 'dk6.ztsc.host:21114' src/common.rs \
  && ok "管理后台 -> ${ADMIN}" || bad "管理后台未替换"
grep -q 'admin\.rustdesk\.com' src/common.rs \
  && bad "仍有 admin.rustdesk.com 残留" || ok "无 admin.rustdesk.com 残留"
grep -qF 'children: []' "$DART" \
  && ok "服务器设置入口已隐藏" || bad "服务器设置入口未隐藏"

# AV1 必须全局禁用：上游只对 32 位禁用，64 位声明可用但不做运行时探测，
# 无 AV1 硬编的设备协商到 AV1 后会永远停在等待传输画面。
if sed -n '/^fn disable_av1() -> bool {/,/^}/p' libs/scrap/src/common/codec.rs | grep -q 'size_of::<usize>() == 4'; then
  bad "AV1 未被禁用（disable_av1 仍按位宽判断）"
else
  ok "AV1 已全局禁用"
fi
if [ "$fail" -ne 0 ]; then
  err "定制校验未通过，拒绝提交（避免发布未定制的构建）。"
  exit 2
fi

log "全部定制校验通过。"

# ---------- 10. 汇报版本 ----------
echo
echo "SYNCED_VERSION=$(grep -m1 '^version *= *"' Cargo.toml | sed -E 's/^version *= *"([^"]+)".*/\1/')"
