"""Generate a fresh RSA private key + self-signed X.509 cert for SF OAuth2.

Produces:
  - sf_private_key_clean.pem  (unencrypted PKCS#8, used by sf_client.py)
  - sf_certificate.pem        (X.509 cert in PEM, upload this to SF)

The cert is self-signed for 5 years. SF only cares about the public key inside,
so CN/O/etc are cosmetic.
"""
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


KEY_PATH = "sf_private_key_clean.pem"
CERT_PATH = "sf_certificate.pem"


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Accenture"),
        x509.NameAttribute(NameOID.COMMON_NAME, "sf-oauth-client"),
    ])

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=5 * 365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open(CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"[OK] Private key -> {KEY_PATH}")
    print(f"[OK] Certificate -> {CERT_PATH}")
    print()
    print("Next: upload sf_certificate.pem to SF Admin Center -> Manage OAuth2 Client Applications -> Edit -> Replace X.509 Certificate")


if __name__ == "__main__":
    main()
