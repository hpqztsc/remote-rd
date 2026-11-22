#!/usr/bin/env python3
"""把 custom-client.json 签成客户端可加载的 custom-client.txt。

格式（对应 src/common.rs::read_custom_client）：
    custom.txt 内容 = base64( ed25519_attached_signature(64B) || json_utf8 )

签名用 RUSTDESK_UPDATE_ED25519_SEED（32 字节 raw 的 canonical base64），
验签侧公钥写死在 src/common.rs 的 CUSTOM_CLIENT_PUBKEY 里。

用法:
    RUSTDESK_UPDATE_ED25519_SEED=... python3 gen-custom-client.py [--check-only]
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "custom-client.json"
OUT_PATH = HERE / "custom-client.txt"

# 与 src/common.rs 的 CUSTOM_CLIENT_PUBKEY 必须一致
PUBKEY_B64 = "2nm83fqNpFX0RG4gIlqPVJSZ5z9Lo/xOrHjgPSvay3o="


def fail(msg):
    print("[gen:error] " + msg, file=sys.stderr)
    raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true", help="只验签现有 custom-client.txt")
    args = ap.parse_args()

    try:
        from nacl import signing
    except ImportError:
        fail("缺少 PyNaCl：pip install pynacl")

    pubkey = base64.b64decode(PUBKEY_B64, validate=True)
    if len(pubkey) != 32:
        fail("公钥不是 32 字节")
    verify_key = signing.VerifyKey(pubkey)

    # ---------- 只校验 ----------
    if args.check_only:
        if not OUT_PATH.is_file():
            fail(f"找不到 {OUT_PATH.name}")
        blob = base64.b64decode(OUT_PATH.read_text().strip(), validate=True)
        try:
            payload = verify_key.verify(blob)          # 剥离签名并校验
        except Exception as exc:
            fail(f"验签失败: {exc}")
        print("验签通过")
        print("配置内容:", json.dumps(json.loads(payload), indent=2, ensure_ascii=False))
        return

    # ---------- 生成 ----------
    seed_b64 = os.environ.get("RUSTDESK_UPDATE_ED25519_SEED", "").strip()
    if not seed_b64:
        fail("缺少环境变量 RUSTDESK_UPDATE_ED25519_SEED")
    seed = base64.b64decode(seed_b64, validate=True)
    if len(seed) != 32:
        fail("seed 不是 32 字节")
    signing_key = signing.SigningKey(seed)
    if bytes(signing_key.verify_key) != pubkey:
        fail("seed 推导的公钥与 CUSTOM_CLIENT_PUBKEY 不一致，拒绝签名")

    config = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        fail("custom-client.json 顶层必须是对象")

    payload = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signed = bytes(signing_key.sign(payload))          # 64B 签名 || payload
    encoded = base64.b64encode(signed).decode("ascii")
    # 固定 LF：仓库里的文件是 LF，Windows 上用默认换行会写成 CRLF 造成假 diff
    OUT_PATH.write_text(encoded + "\n", encoding="utf-8", newline="\n")

    # 自检：确保客户端那边能解出同样的 JSON
    back = json.loads(verify_key.verify(base64.b64decode(encoded, validate=True)))
    if back != config:
        fail("回读内容与源 JSON 不一致")
    print(f"已生成 {OUT_PATH.name}（{len(encoded)} 字节 base64，{len(payload)} 字节 payload）")
    print("键数量:", len(config), "| keys:", ", ".join(sorted(config)))


if __name__ == "__main__":
    main()
