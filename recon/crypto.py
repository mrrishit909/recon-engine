"""Column-level encryption for PII, API keys and bank account metadata (Fernet: AES-128-CBC + HMAC-SHA256)."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.environ.get("RECON_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("RECON_ENCRYPTION_KEY is not set (make one with: python -m recon.crypto)")
    return Fernet(key.encode())


def encrypt(value: str | None) -> bytes | None:
    return None if value is None else _fernet().encrypt(value.encode())


def decrypt(token: bytes | None) -> str | None:
    return None if token is None else _fernet().decrypt(bytes(token)).decode()


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
