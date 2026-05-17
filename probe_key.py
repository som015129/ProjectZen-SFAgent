"""Probe an encrypted SF private key file against common default passwords.

Usage: python probe_key.py sf_private_key.pem
"""
import sys
import os
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

load_dotenv()


def show_file_summary(path: str) -> None:
    with open(path, "rb") as f:
        raw = f.read()
    print(f"File size: {len(raw)} bytes")
    text = raw.decode("utf-8-sig", errors="replace")
    headers = [ln for ln in text.splitlines() if ln.startswith("-----")]
    print("PEM headers found:")
    for h in headers:
        print(f"   {h}")
    print()


def try_passwords(path: str, candidates: list[bytes | None]) -> None:
    with open(path, "rb") as f:
        raw = f.read()

    for pw in candidates:
        label = "<None>" if pw is None else (f"<empty>" if pw == b"" else pw.decode(errors="replace"))
        try:
            key = serialization.load_pem_private_key(raw, password=pw)
            print(f"[OK] Password worked: {label!r}")
            clean = path.replace(".pem", "_clean.pem")
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            with open(clean, "wb") as out:
                out.write(pem)
            print(f"[OK] Wrote unencrypted key to {clean}")
            return
        except Exception as e:
            print(f"[--] {label!r}: {type(e).__name__}: {e}")

    print("\nNo candidate password worked. You'll need to either:")
    print(" - find/remember the password set during cert generation, or")
    print(" - regenerate the X.509 cert in SF Admin Center.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    show_file_summary(path)

    candidates = [
        b"",                                           # empty password (most common)
        None,                                          # no password (truly unencrypted)
        b"changeit",                                   # legacy default
        b"password",
        os.environ.get("SF_CLIENT_ID", "").encode(),   # API key as password
        os.environ.get("SF_COMPANY_ID", "").encode(),  # company id as password
        os.environ.get("SF_USER_ID", "").encode(),     # user id as password
    ]
    # drop empty-from-env entries (e.g. if .env not loaded)
    candidates = [c for c in candidates if c != b""] or candidates
    candidates = [b""] + [c for c in candidates if c != b""]  # always try empty first

    try_passwords(path, candidates)
