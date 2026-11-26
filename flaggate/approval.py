"""Human approval tokens: Ed25519 signatures over the exact plan hash.

The approver private key exists only as an encrypted PKCS#8 PEM (passphrase
never stored). The gatekeeper pins the approver *public* key, so reading every
file on disk is not enough to mint a token.

Token = base64url(canonical payload JSON) + "." + base64url(signature)
Payload: {v, plan_id, plan_hash, base_state_hash, action, env_fp, approver, nonce, iat, exp}
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from flaggate.plan import Plan, plan_hash
from flaggate.state import sha256_hex

ACTIONS = ("apply", "rollback")


class ApprovalError(Exception):
    """Token missing, malformed, or not signed by the pinned approver key."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# --------------------------------------------------------------------------- keys
def generate_keypair(priv_path: Path, pub_path: Path, passphrase: str) -> str:
    """Write an encrypted private key (0600) and the raw public key (base64). Returns the key fingerprint."""
    if len(passphrase) < 8:
        raise ValueError("passphrase must be at least 8 characters")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(passphrase.encode()))
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_path.write_text(_b64e(raw) + "\n")
    return fingerprint(raw)


def load_private_key(priv_path: Path, passphrase: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(priv_path.read_bytes(), passphrase.encode())
    except (ValueError, TypeError) as e:
        raise ApprovalError("wrong passphrase or unreadable approver key") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise ApprovalError("approver key is not Ed25519")
    return key


def load_public_key(pub_path: Path) -> bytes:
    return _b64d(pub_path.read_text().strip())


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(pub_raw: bytes) -> str:
    return "ed25519:" + sha256_hex(pub_raw)[:16]


# --------------------------------------------------------------------------- tokens
def make_payload(plan: Plan, action: str, approver: str, ttl_min: int, now: float | None = None,
                 base_state_hash: str | None = None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    now = time.time() if now is None else now
    return {
        "v": 1,
        "plan_id": plan.plan_id,
        "plan_hash": plan_hash(plan),
        "base_state_hash": base_state_hash or plan.base_state_hash,
        "action": action,
        "env_fp": plan.target.env_fp,
        "approver": approver,
        "nonce": "n_" + secrets.token_hex(12),
        "iat": int(now),
        "exp": int(now + ttl_min * 60),
    }


def sign_token(key: Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    body = _canon(payload)
    return _b64e(body) + "." + _b64e(key.sign(body))


def verify_token(pub_raw: bytes, token: str | None) -> dict[str, Any]:
    """Return the payload if ``token`` is signed by ``pub_raw``; raise ApprovalError otherwise."""
    if not token:
        raise ApprovalError("no approval token")
    try:
        body_b64, sig_b64 = token.strip().split(".")
        body, sig = _b64d(body_b64), _b64d(sig_b64)
    except ValueError as e:
        raise ApprovalError("malformed approval token") from e
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, body)
    except InvalidSignature as e:
        raise ApprovalError("bad signature (not signed by the pinned approver key)") from e
    payload = json.loads(body)
    if payload.get("v") != 1:
        raise ApprovalError("unsupported token version")
    return payload
