#!/usr/bin/env python3
"""为已发布的 Release 生成并上传 rustdesk-update.json 与其 ed25519 签名。

签名与清单格式完全交给上游的 res/update/generate_update_metadata.py，
本脚本只负责：找出发布里实际存在的安装包 → 下载 → 调用上游工具签名 → 上传。

这样上游若调整清单 schema，随源码同步即可自动跟进，不需要我们改代码。

用法:
    python3 publish-update-manifest.py --version 1.5.3 [--repo owner/name] [--dry-run]

所需环境变量:
    RUSTDESK_UPDATE_ED25519_SEED          32 字节 seed 的 canonical base64（仓库 secret）
    RUSTDESK_UPDATE_ED25519_PUBLIC_KEY    32 字节公钥的 canonical base64（仓库 variable）
    GH_TOKEN                              上传用（Actions 内置 token 即可）
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# (platform, arch, format, 候选资产名模板) —— 上游不同版本打包命名略有差异，按序取第一个存在的
TARGETS = [
    ("windows", "x86_64", "exe", ["rustdesk-{v}-x86_64.exe"]),
    ("windows", "x86_64", "msi", ["rustdesk-{v}-x86_64.msi"]),
    ("windows", "aarch64", "exe", ["rustdesk-{v}-aarch64.exe"]),
    ("windows", "aarch64", "msi", ["rustdesk-{v}-aarch64.msi"]),
    ("windows", "x86", "exe", ["rustdesk-{v}-x86-sciter.exe"]),
    ("macos", "x86_64", "dmg", ["rustdesk-{v}-x86_64.dmg", "rustdesk-{v}-x86_64-x86_64.dmg"]),
    ("macos", "aarch64", "dmg", ["rustdesk-{v}-aarch64.dmg", "rustdesk-{v}-aarch64-aarch64.dmg"]),
]

GENERATOR = "res/update/generate_update_metadata.py"


def log(msg):
    print(msg, flush=True)


def api_get(url, token):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "remote-rd-update-manifest")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _download_once(url, dest, token):
    if shutil.which("curl"):
        cmd = ["curl", "-fsSL", "--retry", "5", "--retry-all-errors",
               "--retry-delay", "3", "--max-time", "1800", "-o", dest]
        if token:
            cmd += ["-H", f"Authorization: Bearer {token}"]
        cmd.append(url)
        subprocess.run(cmd, check=True)
        return
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "remote-rd-update-manifest")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def download(url, dest, token, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            _download_once(url, dest, token)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return
            raise RuntimeError("下载结果为空")
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            if attempt == attempts:
                raise
            log(f"    下载中断（{exc}），重试 {attempt}/{attempts - 1}")
            time.sleep(3 * attempt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="发布 tag（纯版本号，如 1.5.3）")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "hpqztsc/remote-rd"))
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--dry-run", action="store_true", help="只生成文件，不上传")
    args = ap.parse_args()

    version = args.version.lstrip("v")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if not os.path.exists(GENERATOR):
        raise SystemExit(f"找不到上游签名工具 {GENERATOR}（请在仓库根目录运行）")

    log(f"读取 Release: {args.repo}@{version}")
    try:
        release = api_get(f"https://api.github.com/repos/{args.repo}/releases/tags/{version}", token)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"找不到 Release {version}: {exc}")
    assets = {a["name"]: a for a in release.get("assets", [])}
    log(f"发布资产数量: {len(assets)}")

    artifact_args = []   # sign 用：--artifact 平台 架构 格式 路径
    artifact_paths = []  # verify 用：--artifact 只收单个本地路径
    base_url = f"https://github.com/{args.repo}/releases/download"
    workspace = tempfile.mkdtemp(prefix="update-manifest-")
    art_dir = os.path.join(workspace, "artifacts")
    os.makedirs(art_dir, exist_ok=True)

    try:
        for platform, arch, fmt, candidates in TARGETS:
            chosen = next((c.format(v=version) for c in candidates
                           if c.format(v=version) in assets), None)
            if not chosen:
                log(f"  跳过（发布中不存在）{platform}/{arch}/{fmt}")
                continue
            dest = os.path.join(art_dir, chosen)
            log(f"  下载 {chosen} ({assets[chosen].get('size', 0) / 1048576:.1f} MB)")
            download(assets[chosen]["browser_download_url"], dest, token)
            artifact_args += ["--artifact", platform, arch, fmt, dest]
            artifact_paths.append(dest)

        if not artifact_args:
            raise SystemExit("没有可写入清单的安装包，终止")

        meta_path = os.path.join(args.out_dir, "rustdesk-update.json")
        sig_path = os.path.join(args.out_dir, "rustdesk-update.json.sig")
        published_at = release.get("published_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        cmd = [sys.executable, GENERATOR, "sign",
               *artifact_args,
               "--version", version,
               "--release-id", version,
               "--package-id", "rustdesk",
               "--release-base-url", base_url,
               "--published-at", published_at,
               "--metadata-out", meta_path,
               "--signature-out", sig_path]
        log("调用上游工具签名…")
        subprocess.run(cmd, check=True)

        # 自检：用同一个工具验证刚生成的清单
        # 注意：verify 的 --artifact 只接收本地文件路径（sign 收 平台/架构/格式/路径 四元组），
        # 两者参数形态不同，不能共用同一份参数列表。
        verify_args = []
        for path in artifact_paths:
            verify_args += ["--artifact", path]
        subprocess.run([sys.executable, GENERATOR, "verify",
                        "--metadata", meta_path,
                        "--signature", sig_path,
                        *verify_args,
                        "--version", version,
                        "--release-id", version,
                        "--package-id", "rustdesk",
                        "--release-base-url", base_url],
                       check=True)
        log("自检通过")

        if args.dry_run:
            log("--dry-run：跳过上传")
            return
        if not shutil.which("gh"):
            raise SystemExit("找不到 gh，无法上传")
        subprocess.run(["gh", "release", "upload", version, meta_path, sig_path,
                        "--repo", args.repo, "--clobber"], check=True)
        log("已上传 rustdesk-update.json 与 .sig 到 Release")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
