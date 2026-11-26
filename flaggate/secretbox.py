"""Gatekeeper secrets at rest: scrypt(passphrase) -> AES-256-GCM. Only the gatekeeper decrypts them."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

N, R, P = 2**15, 8, 1


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=N, r=R, p=P).derive(passphrase.encode())


def seal(path: Path, data: dict[str, Any], passphrase: str) -> None:
    if len(passphrase) < 8:
        raise ValueError("passphrase must be at least 8 characters")
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(data).encode(), b"flaggate-secrets-v1")
    blob = {"v": 1, "kdf": "scrypt", "n": N, "r": R, "p": P,
            **{k: base64.b64encode(v).decode() for k, v in (("salt", salt), ("nonce", nonce), ("ct", ct))}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(blob, f)
    os.replace(tmp, path)


def unseal(path: Path, passphrase: str) -> dict[str, Any]:
    blob = json.loads(path.read_text())
    salt, nonce, ct = (base64.b64decode(blob[k]) for k in ("salt", "nonce", "ct"))
    try:
        return json.loads(AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, b"flaggate-secrets-v1"))
    except InvalidTag as e:
        raise ValueError("wrong gatekeeper passphrase") from e
