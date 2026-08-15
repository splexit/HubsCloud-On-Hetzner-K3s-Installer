from __future__ import annotations

from pathlib import Path

from config import instance_dir
from schemas.models import RestoreBundleManifest


def restore_bundle_dir(instance_id: str) -> Path:
    return instance_dir(instance_id) / "restore-bundle"


def manifest_path(instance_id: str) -> Path:
    return restore_bundle_dir(instance_id) / "manifest.json"


def load_restore_bundle_manifest(instance_id: str) -> RestoreBundleManifest | None:
    path = manifest_path(instance_id)
    if not path.exists():
        return None
    return RestoreBundleManifest.model_validate_json(path.read_text())


def has_restore_bundle(instance_id: str) -> bool:
    manifest = load_restore_bundle_manifest(instance_id)
    if not manifest:
        return False
    dump = restore_bundle_dir(instance_id) / manifest.db_backup_filename
    secrets = restore_bundle_dir(instance_id) / "secrets.json"
    return dump.exists() and secrets.exists()


def local_restore_dump_path(instance_id: str) -> Path | None:
    manifest = load_restore_bundle_manifest(instance_id)
    if not manifest:
        return None
    dump = restore_bundle_dir(instance_id) / manifest.db_backup_filename
    return dump if dump.exists() else None
