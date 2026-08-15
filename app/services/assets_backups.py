from __future__ import annotations

import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import instance_dir
from pipeline.core import kube_env, run_cmd
from schemas.models import AssetsBackupEntry, AssetsBackupsList, HCCE_NAMESPACE

RETICULUM_APP = "reticulum"
STORAGE_MOUNT = "/storage"
RECENT_DAYS = 7
MIN_BACKUP_BYTES = 100
ASSETS_NAME_RE = re.compile(r"^assets_backup-(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})\.tar\.gz$")


class AssetsBackupError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def assets_backups_dir(instance_id: str) -> Path:
    return instance_dir(instance_id) / "assets-backups"


def _ensure_kubeconfig(instance_id: str) -> None:
    if not (instance_dir(instance_id) / "kubeconfig").exists():
        raise AssetsBackupError(
            "Cluster kubeconfig not available — asset backups require a running cluster",
            status_code=503,
        )


def _parse_entry(path: Path) -> AssetsBackupEntry | None:
    m = ASSETS_NAME_RE.match(path.name)
    if not m:
        return None
    day, hour, minute = m.group(1), m.group(2), m.group(3)
    try:
        created = datetime.strptime(f"{day} {hour}:{minute}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    stat = path.stat()
    return AssetsBackupEntry(
        filename=path.name,
        size_bytes=stat.st_size,
        backup_date=day,
        backup_time=f"{hour}:{minute}",
        created_at=created.isoformat(),
    )


def _list_local_entries(instance_id: str) -> list[AssetsBackupEntry]:
    out_dir = assets_backups_dir(instance_id)
    if not out_dir.exists():
        return []
    entries: list[AssetsBackupEntry] = []
    for path in out_dir.glob("assets_backup-*.tar.gz"):
        entry = _parse_entry(path)
        if entry:
            entries.append(entry)
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def _reticulum_pod_name(instance_id: str) -> str:
    env = kube_env(instance_id)
    result = run_cmd(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            HCCE_NAMESPACE,
            "-l",
            f"app={RETICULUM_APP}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        raise AssetsBackupError(f"Could not find Reticulum pod: {err}", status_code=502)
    pod = (result.stdout or "").strip()
    if not pod:
        raise AssetsBackupError("Reticulum pod not found — is HCCE running?", status_code=502)

    ready = run_cmd(
        [
            "kubectl",
            "get",
            "pod",
            pod,
            "-n",
            HCCE_NAMESPACE,
            "-o",
            "jsonpath={.status.containerStatuses[0].ready}",
        ],
        env=env,
        timeout=30,
    )
    if (ready.stdout or "").strip() != "true":
        raise AssetsBackupError("Reticulum is not ready yet", status_code=409)
    return pod


def _stream_tar_from_reticulum(instance_id: str, pod: str, dest: Path) -> None:
    """Archive /storage from the running Reticulum pod — no sidecar or second PVC mount."""
    env = kube_env(instance_id)
    partial = dest.with_suffix(dest.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        partial.unlink()
    try:
        with partial.open("wb") as out:
            proc = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    pod,
                    "-n",
                    HCCE_NAMESPACE,
                    "--",
                    "tar",
                    "cz",
                    "-C",
                    STORAGE_MOUNT,
                    ".",
                ],
                env=env,
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=3600,
                check=False,
            )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            partial.unlink(missing_ok=True)
            raise AssetsBackupError(
                f"Could not archive Reticulum storage: {err or 'tar failed'}",
                status_code=502,
            )
        size = partial.stat().st_size
        if size < MIN_BACKUP_BYTES:
            partial.unlink(missing_ok=True)
            raise AssetsBackupError(
                f"Asset archive too small ({size} bytes) — is /storage empty?",
                status_code=502,
            )
        partial.rename(dest)
    except subprocess.TimeoutExpired as e:
        partial.unlink(missing_ok=True)
        raise AssetsBackupError("Timed out archiving Reticulum assets", status_code=504) from e


def run_assets_backup_now(instance_id: str) -> AssetsBackupEntry:
    _ensure_kubeconfig(instance_id)
    pod = _reticulum_pod_name(instance_id)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M")
    filename = f"assets_backup-{stamp}.tar.gz"
    dest = assets_backups_dir(instance_id) / filename
    _stream_tar_from_reticulum(instance_id, pod, dest)
    entry = _parse_entry(dest)
    if not entry:
        raise AssetsBackupError("Asset backup saved but could not parse metadata", status_code=500)
    return entry


def list_assets_backups(instance_id: str) -> AssetsBackupsList:
    kube_ok = (instance_dir(instance_id) / "kubeconfig").exists()
    all_entries = _list_local_entries(instance_id)
    cutoff = date.today() - timedelta(days=RECENT_DAYS - 1)
    recent = [e for e in all_entries if e.backup_date >= cutoff.isoformat()]
    latest = all_entries[0] if all_entries else None
    message = ""
    if not all_entries and kube_ok:
        message = "No asset archives saved yet — create one to download Reticulum uploads from ret-pvc."
    elif not kube_ok:
        message = "Cluster not reachable — showing any archives saved under ./data on this machine."
    return AssetsBackupsList(
        available=kube_ok,
        message=message,
        latest=latest,
        recent=recent,
        all_count=len(all_entries),
    )


def resolve_assets_backup(
    instance_id: str,
    *,
    latest: bool = False,
    filename: str | None = None,
) -> tuple[AssetsBackupEntry, Path]:
    entries = _list_local_entries(instance_id)
    if not entries:
        raise AssetsBackupError("No asset backups saved yet", status_code=404)
    if filename:
        name = Path(filename).name
        if not ASSETS_NAME_RE.match(name):
            raise AssetsBackupError("Invalid asset backup filename", status_code=400)
        for entry in entries:
            if entry.filename == name:
                path = assets_backups_dir(instance_id) / name
                if path.exists():
                    return entry, path
        raise AssetsBackupError(f"Asset backup not found: {name}", status_code=404)
    if latest:
        path = assets_backups_dir(instance_id) / entries[0].filename
        return entries[0], path
    raise AssetsBackupError("Specify latest or filename", status_code=400)
