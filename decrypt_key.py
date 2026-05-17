"""Decrypt a password-protected SF private key to unencrypted PKCS#8 PEM.

Usage:
    python decrypt_key.py sf_private_key.pem sf_private_key_clean.pem
"""
import sys
import getpass
from cryptography.hazmat.primitives import serialization


def main(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        raw = f.read()

    password = getpass.getpass("Private key password: ").encode()

    key = serialization.load_pem_private_key(raw, password=password)

    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with open(dst, "wb") as f:
        f.write(pem)

    print(f"OK -> wrote unencrypted PKCS#8 key to {dst}")
    print(f"First line: {pem.splitlines()[0].decode()}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
