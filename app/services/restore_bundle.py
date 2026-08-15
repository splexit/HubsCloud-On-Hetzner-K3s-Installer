from __future__ import annotations

import io
import shutil
import zipfile
from datetime import datetime, timezone

from config import instance_dir
from schemas.models import RestoreBundleManifest
from services.restore_bundle_paths import (
    has_restore_bundle,
    load_restore_bundle_manifest,
    manifest_path,
    restore_bundle_dir,
)
from services.storage import append_log, load_secrets, load_spec


def _write_readme(instance_id: str, manifest: RestoreBundleManifest) -> None:
    spec = load_spec(instance_id)
    progress = f"http://127.0.0.1:8080/instances/{instance_id}"
    lines = [
        "Hubs Cloud installer — local restore bundle",
        "==========================================",
        "",
        "This folder lets you recreate the hub WITHOUT Hetzner block volumes.",
        "Secrets, SSH keys, and a PostgreSQL dump are kept together so you do",
        "not need to manually match secrets to an old database.",
        "",
        f"Instance ID:  {instance_id}",
        f"Hub domain:   {spec.hub_domain or '—'}",
        f"Created:      {manifest.created_at}",
        f"DB backup:    {manifest.db_backup_filename} ({manifest.db_backup_date} UTC)",
        "",
        "What is included",
        "----------------",
        "  manifest.json           — this bundle metadata",
        "  secrets.json            — generated HCCE secrets (required for DB restore)",
        "  spec.json               — installer configuration snapshot",
        "  volumes-inventory.json  — Hetzner volume IDs (if volumes still exist)",
        "  ssh/                    — provisioning SSH key pair",
        "  db_backup-*.gz          — PostgreSQL dump from pgsql-backup CronJob",
        "",
        "What is NOT included (yet)",
        "--------------------------",
        "  Reticulum uploads/assets (ret-pvc) — use volume reattach when possible.",
        "",
        "Recreate workflow",
        "-----------------",
        f"1. Open {progress} (same ./data volume mounted in the installer container)",
        "2. Recreate cluster with NEW volumes (do not reattach if volumes are gone)",
        "3. Enable “Restore database from local bundle” on review, then provision",
        "4. Installer applies the dump after HCCE deploys on the fresh Postgres volume",
        "",
    ]
    (restore_bundle_dir(instance_id) / "README.txt").write_text("\n".join(lines))


def create_restore_bundle(
    instance_id: str,
    *,
    latest: bool = False,
    backup_date: str | None = None,
    backup_filename: str | None = None,
) -> RestoreBundleManifest:
    """Pull a DB dump from the live cluster and snapshot recreation metadata locally."""
    from services.db_backups import DbBackupError, fetch_backup_for_download

    spec = load_spec(instance_id)
    if not load_secrets(instance_id):
        raise DbBackupError("secrets.json missing — cannot build restore bundle", status_code=400)

    entry, tmp_dump = fetch_backup_for_download(
        instance_id,
        latest=latest or (not backup_date and not backup_filename),
        backup_date=backup_date,
        filename=backup_filename,
    )

    out_dir = restore_bundle_dir(instance_id)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    dest_dump = out_dir / entry.filename
    shutil.move(str(tmp_dump), dest_dump)

    inst = instance_dir(instance_id)
    shutil.copy2(inst / "secrets.json", out_dir / "secrets.json")
    shutil.copy2(inst / "spec.json", out_dir / "spec.json")
    inv = inst / "volumes-inventory.json"
    if inv.exists():
        shutil.copy2(inv, out_dir / "volumes-inventory.json")
    ssh_src = inst / "ssh"
    if ssh_src.exists():
        shutil.copytree(ssh_src, out_dir / "ssh")

    manifest = RestoreBundleManifest(
        instance_id=instance_id,
        hub_domain=spec.hub_domain,
        created_at=datetime.now(timezone.utc).isoformat(),
        db_backup_filename=entry.filename,
        db_backup_date=entry.backup_date,
        db_backup_time=entry.backup_time,
        db_backup_size_bytes=entry.size_bytes,
        includes_reticulum_assets=False,
    )
    manifest_path(instance_id).write_text(manifest.model_dump_json(indent=2))
    _write_readme(instance_id, manifest)

    append_log(
        instance_id,
        f"Local restore bundle saved — {entry.filename} ({entry.size_bytes} bytes) "
        f"with secrets and spec under restore-bundle/",
    )
    return manifest


def build_restore_bundle_zip(instance_id: str) -> bytes:
    from services.db_backups import DbBackupError

    manifest = load_restore_bundle_manifest(instance_id)
    if not manifest or not has_restore_bundle(instance_id):
        raise DbBackupError("No restore bundle on disk for this instance", status_code=404)

    buf = io.BytesIO()
    root = restore_bundle_dir(instance_id)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    return buf.getvalue()
