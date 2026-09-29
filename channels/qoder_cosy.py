"""channels/qoder_cosy.py — Qoder 协议原语：请求编码 + COSY 签名。

移植自 wild-work/internal/qoder（encoding.go + cosy.go，均为社区逆向成果）。

QoderEncoding：base64 → 三段重排 → 自定义字母表映射（'=' → '$'）。
COSY 签名：
  - tempKey 为 16 个 ASCII 字符（AES-128 密钥，与桌面端一致）；
  - cosy-key = base64(RSA_PKCS1_v1_5(tempKey))（服务端公钥硬编码于桌面客户端）；
  - info = base64(AES-128-CBC(identity 的排序紧凑 JSON))，key=iv=tempKey；
  - Authorization = "Bearer COSY." + base64(payload) + "." + md5(payload + cosyKey + date + body + path)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 服务端 RSA 公钥（桌面客户端硬编码，与 wild-work 一致）
SERVER_PUBKEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----
"""

CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM_PAD = "$"
COSY_VERSION = "1.0.10"


def qoder_encode(plain: bytes) -> str:
    """base64 → 三段重排 → 字符映射（'=' → '$'）。"""
    std = base64.b64encode(plain).decode()
    n = len(std)
    if n == 0:
        return ""
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    out = []
    for c in rearranged:
        if c == "=":
            out.append(CUSTOM_PAD)
        else:
            out.append(CUSTOM_ALPHABET[STD_ALPHABET.index(c)])
    return "".join(out)


def qoder_decode(enc: str) -> bytes:
    """逆编码：字符映射（'$' → '='）→ 三段逆重排 → base64 解码。"""
    if not enc:
        return b""
    mapped = []
    for c in enc:
        if c == CUSTOM_PAD:
            mapped.append("=")
            continue
        idx = CUSTOM_ALPHABET.find(c)
        if idx < 0:
            raise ValueError(f"invalid char {c!r}")
        mapped.append(STD_ALPHABET[idx])
    n = len(mapped)
    a = n // 3
    std = "".join(mapped[n - a:]) + "".join(mapped[a:n - a]) + "".join(mapped[:a])
    return base64.b64decode(std)


def _sorted_compact(m: dict) -> str:
    """按 key 排序、无空白序列化（COSY 签名对字节敏感）。"""
    return "{" + ",".join(
        json.dumps(k, ensure_ascii=False) + ":" + json.dumps(m[k], ensure_ascii=False)
        for k in sorted(m)
    ) + "}"


def _aes_cbc_encrypt(plain: bytes, key: bytes) -> bytes:
    pad_len = 16 - len(plain) % 16
    padded = plain + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return enc.update(padded) + enc.finalize()


def new_uuid4() -> str:
    return str(uuid.uuid4())


def _uuid_hex(n: int) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


class CosySession:
    """每账号每轮请求的 COSY 签名会话。"""

    def __init__(self, machine_id: str, machine_token: str, machine_type: str,
                 nickname: str, uid: str, dt: str, drt: str, user_type: str = ""):
        if not machine_id or not machine_token or not machine_type:
            raise ValueError("missing machine fingerprint (need MachineID/Token/Type)")
        self.machine_id = machine_id
        self.machine_token = machine_token
        self.machine_type = machine_type
        self.uid = uid

        temp_key = _uuid_hex(16).encode()  # 16 ASCII 字符
        pub = serialization.load_pem_public_key(SERVER_PUBKEY_PEM)
        wrapped = pub.encrypt(temp_key, padding.PKCS1v15())
        self.temp_key = temp_key
        self.cosy_key = base64.b64encode(wrapped).decode()

        identity = {
            "name": nickname or "",
            "aid": uid,
            "uid": uid,
            "yx_uid": "",
            "organization_id": "",
            "organization_name": "",
            "user_type": user_type or "personal_standard",
            "security_oauth_token": dt,
            "refresh_token": drt,
        }
        self.info = base64.b64encode(
            _aes_cbc_encrypt(_sorted_compact(identity).encode(), temp_key)
        ).decode()

    def auth_header(self, body: str, raw_url: str) -> str:
        payload = {
            "cosyVersion": COSY_VERSION,
            "ideVersion": "",
            "info": self.info,
            "requestId": new_uuid4(),
            "version": "v1",
        }
        payload_b64 = base64.b64encode(_sorted_compact(payload).encode()).decode()
        path = raw_url.split("?", 1)[0]
        # pathSig：去掉 /algo 前缀
        from urllib.parse import urlparse
        p = urlparse(raw_url).path
        path_sig = p[len("/algo"):] if p.startswith("/algo") else p
        date = str(int(time.time()))
        sig_input = "\n".join([payload_b64, self.cosy_key, date, body, path_sig])
        sig = hashlib.md5(sig_input.encode()).hexdigest()
        return "Bearer COSY." + payload_b64 + "." + sig

    def apply_headers(self, headers: dict, body: str, raw_url: str,
                      accept: str = "application/json", model_key: str = "") -> dict:
        headers.update({
            "cosy-data-policy": "agree",
            "content-type": "application/json",
            "cosy-machinetype": self.machine_type,
            "cosy-clienttype": "5",
            "cosy-date": str(int(time.time())),
            "cosy-user": self.uid,
            "cosy-key": self.cosy_key,
            "cache-control": "no-cache",
            "accept": accept,
            "authorization": self.auth_header(body, raw_url),
            "cosy-version": COSY_VERSION,
            "cosy-machineid": self.machine_id,
            "cosy-machinetoken": self.machine_token,
            "login-version": "v2",
            "user-agent": "Go-http-client/2.0",
            "cosy-scene": "assistant",
            "cosy-business-product": "ide",
            "cosy-business-type": "agent",
        })
        if model_key:
            headers["x-model-key"] = model_key
            headers["x-model-source"] = "system"
        return headers


def ensure_fingerprint(acc) -> None:
    """为账号生成/保留 COSY 机器指纹（幂等）。"""
    if not acc.extra.get("machineId"):
        acc.extra["machineId"] = new_uuid4()
    if not acc.extra.get("machineToken"):
        seed = (new_uuid4() + new_uuid4()).encode()[:50]
        acc.extra["machineToken"] = base64.urlsafe_b64encode(seed).decode().rstrip("=")
    if not acc.extra.get("machineType"):
        acc.extra["machineType"] = new_uuid4().replace("-", "")[:18]
