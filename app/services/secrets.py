from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from schemas.models import InstanceSpec

# Required for HCCE + Postgres to match data on reattached CSI volumes.
REATTACH_SECRET_KEYS = ("db_password", "perms_key", "pgrst_jwt_secret", "k3s_token", "node_cookie", "guardian_key", "phx_key")


def secrets_available_for_reattach(instance_id: str) -> bool:
    from services.storage import load_secrets

    secrets = load_secrets(instance_id)
    return bool(secrets) and all(secrets.get(k) for k in REATTACH_SECRET_KEYS)


def generate_secrets(hub_domain: str = "") -> dict[str, str]:
    perms_key, pgrst_jwt_secret = generate_rsa_material()
    out = {
        "k3s_token": secrets.token_hex(32),
        "db_password": secrets.token_urlsafe(24),
        "node_cookie": secrets.token_hex(16),
        "guardian_key": secrets.token_hex(24),
        "phx_key": secrets.token_hex(24),
        "admin_password": secrets.token_urlsafe(16),
        "perms_key": perms_key,
        "pgrst_jwt_secret": pgrst_jwt_secret,
    }
    if hub_domain:
        init_cert, init_key = generate_self_signed_cert(hub_domain)
        out["init_cert"] = init_cert
        out["init_key"] = init_key
    return out


def generate_self_signed_cert(hub_domain: str) -> tuple[str, str]:
    """Return (init_cert, init_key): base64 PEM for the bootstrap TLS Secret.

    Mirrors generateCertificate() in hubs-cloud community-edition
    generate_script/index.js, which upstream substitutes as $initCert/$initKey:
    a dedicated RSA-2048 keypair (deliberately NOT the PERMS_KEY pair), a
    self-signed certificate with CN set to the hub domain, valid one year.

    This must be generated per instance. A previous version of hcce.yaml.j2
    carried the literal output of one upstream run, so every cluster shipped the
    same private key under a CN that never matched its own domain.
    """
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hub_domain)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hub_domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    # node-forge's privateKeyToPem emits PKCS#1 ("RSA PRIVATE KEY"); match it.
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    import base64

    return (
        base64.b64encode(cert_pem).decode(),
        base64.b64encode(key_pem).decode(),
    )


def generate_rsa_material() -> tuple[str, str]:
    """Return (PERMS_KEY, PGRST_JWT_SECRET) in the same single-line format as k3s-setup/hcce/hcce.yaml."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # Production hcce.yaml stores PEM as one line with \\n (two chars: backslash + backslash + n).
    # Turkey substitutes this into config.toml; a single \n would become a real newline and break TOML.
    escaped = private_pem.replace("\n", "\\\\n")
    public_numbers = key.public_key().public_numbers()
    n = public_numbers.n
    e = public_numbers.e
    import base64

    def b64url_uint(val: int) -> str:
        b = val.to_bytes((val.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    jwt_secret = json.dumps({"kty": "RSA", "n": b64url_uint(n), "e": b64url_uint(e)})
    return escaped, jwt_secret


def write_ssh_keypair(instance_dir: Path) -> tuple[str, str]:
    ssh_dir = instance_dir / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    pub_path = ssh_dir / "id_ed25519.pub"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
        check=True,
    )
    private_key = key_path.read_text()
    public_key = pub_path.read_text().strip()
    key_path.chmod(0o600)
    return public_key, private_key
