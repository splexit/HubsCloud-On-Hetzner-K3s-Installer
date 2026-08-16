from __future__ import annotations

import json
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import instance_dir
from pipeline.core import kube_env, run_cmd
from schemas.models import HCCE_NAMESPACE, DbBackupEntry, DbBackupsList

READER_POD = "hcce-backup-reader"
BACKUP_DIR = "/backups"
BACKUP_KEEP_DAYS = 14
RECENT_DAYS = 7
BACKUP_NAME_RE = re.compile(r"^db_backup-(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})\.gz$")
# busybox in the reader pod — GNU stat -c is not available
BACKUP_LIST_SCRIPT = (
    f"for f in {BACKUP_DIR}/db_backup-*.gz; do "
    '[ -f "$f" ] || continue; '
    'echo "$(wc -c < "$f" | tr -d " ") $f"; '
    "done"
)


class DbBackupError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _kubeconfig_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / "kubeconfig"


def _ensure_kubeconfig(instance_id: str) -> None:
    if not _kubeconfig_path(instance_id).exists():
        raise DbBackupError(
            "Cluster kubeconfig not available — backups can be downloaded while the cluster is running",
            status_code=503,
        )


def _parse_backup_entry(filename: str, size_bytes: int) -> DbBackupEntry | None:
    m = BACKUP_NAME_RE.match(filename.strip())
    if not m:
        return None
    day, hour, minute = m.group(1), m.group(2), m.group(3)
    try:
        created = datetime.strptime(f"{day} {hour}:{minute}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return DbBackupEntry(
        filename=filename,
        size_bytes=size_bytes,
        backup_date=day,
        backup_time=f"{hour}:{minute}",
        created_at=created.isoformat(),
    )


def _backup_job_running(instance_id: str) -> bool:
    env = kube_env(instance_id)
    result = run_cmd(
        ["kubectl", "get", "pods", "-n", HCCE_NAMESPACE, "-o", "json"],
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        return False
    items = json.loads(result.stdout).get("items", [])
    for pod in items:
        name = pod.get("metadata", {}).get("name", "")
        phase = (pod.get("status") or {}).get("phase", "")
        if name.startswith("pgsql-backup-") and phase in ("Running", "Pending"):
            return True
    return False


def _reader_pod_phase(instance_id: str) -> str | None:
    env = kube_env(instance_id)
    result = run_cmd(
        [
            "kubectl",
            "get",
            "pod",
            READER_POD,
            "-n",
            HCCE_NAMESPACE,
            "-o",
            "jsonpath={.status.phase}",
        ],
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    phase = (result.stdout or "").strip()
    return phase or None


def _delete_reader_pod(instance_id: str, *, wait: bool = True) -> None:
    env = kube_env(instance_id)
    args = ["kubectl", "delete", "pod", READER_POD, "-n", HCCE_NAMESPACE, "--ignore-not-found"]
    if wait:
        args.extend(["--wait=true", "--timeout=120s"])
    else:
        args.append("--wait=false")
    run_cmd(args, env=env, timeout=150)


def _wait_reader_pod_gone(instance_id: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _reader_pod_phase(instance_id) is None:
            return
        time.sleep(2)
    raise DbBackupError(
        "Timed out waiting for backup reader pod to release pgsql-backups-pvc",
        status_code=504,
    )


def _release_backup_volume(instance_id: str) -> None:
    """Ensure hcce-backup-reader is gone so pgsql-backup jobs can mount the RWO volume."""
    _delete_reader_pod(instance_id, wait=True)
    _wait_reader_pod_gone(instance_id)


def _create_reader_pod(instance_id: str) -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": READER_POD,
            "namespace": HCCE_NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "hubs-installer",
                "app": "hcce-backup-reader",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"workload-type": "database"},
            "tolerations": [
                {
                    "key": "node-role.kubernetes.io/control-plane",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "key": "node-role.kubernetes.io/master",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
            ],
            "containers": [
                {
                    "name": "reader",
                    "image": "busybox:1.36",
                    "command": ["sleep", "3600"],
                    "volumeMounts": [{"name": "backups", "mountPath": BACKUP_DIR, "readOnly": True}],
                }
            ],
            "volumes": [
                {
                    "name": "backups",
                    "persistentVolumeClaim": {"claimName": "pgsql-backups-pvc"},
                }
            ],
        },
    }
    env = kube_env(instance_id)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        tmp.write(json.dumps(manifest))
        manifest_path = tmp.name
    try:
        apply = run_cmd(["kubectl", "apply", "-f", manifest_path], env=env, timeout=60)
        if apply.returncode != 0:
            raise DbBackupError(
                f"Could not mount backup volume: {(apply.stderr or apply.stdout).strip()}",
                status_code=502,
            )
    finally:
        Path(manifest_path).unlink(missing_ok=True)

    deadline = time.time() + 90
    while time.time() < deadline:
        phase = _reader_pod_phase(instance_id)
        if phase == "Running":
            return
        if phase == "Failed":
            raise DbBackupError("Backup reader pod failed to start", status_code=502)
        time.sleep(2)
    raise DbBackupError("Timed out waiting for backup volume reader pod", status_code=504)


@contextmanager
def _reader_pod(instance_id: str):
    if _backup_job_running(instance_id):
        raise DbBackupError(
            "PostgreSQL backup job is running — try again in a few minutes",
            status_code=409,
        )
    _release_backup_volume(instance_id)
    _create_reader_pod(instance_id)
    try:
        yield
    finally:
        _release_backup_volume(instance_id)


def _list_backup_files(instance_id: str) -> list[DbBackupEntry]:
    env = kube_env(instance_id)
    with _reader_pod(instance_id):
        result = run_cmd(
            [
                "kubectl",
                "exec",
                READER_POD,
                "-n",
                HCCE_NAMESPACE,
                "--",
                "sh",
                "-c",
                BACKUP_LIST_SCRIPT,
            ],
            env=env,
            timeout=120,
        )
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        raise DbBackupError(f"Could not list backups: {err}", status_code=502)

    entries: list[DbBackupEntry] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            size_str, path = line.split(" ", 1)
            name = Path(path).name
            entry = _parse_backup_entry(name, int(size_str))
            if entry:
                entries.append(entry)
        except ValueError:
            continue

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def run_db_backup_now(instance_id: str) -> DbBackupEntry:
    """Trigger the pgsql-backup CronJob once and wait for the dump on the backup volume."""
    _ensure_kubeconfig(instance_id)
    if _backup_job_running(instance_id):
        raise DbBackupError(
            "PostgreSQL backup job is already running — try again in a few minutes",
            status_code=409,
        )

    _release_backup_volume(instance_id)
    env = kube_env(instance_id)
    job_name = f"pgsql-backup-manual-{int(time.time())}"
    create = run_cmd(
        [
            "kubectl",
            "create",
            "job",
            job_name,
            "-n",
            HCCE_NAMESPACE,
            "--from=cronjob/pgsql-backup",
        ],
        env=env,
        timeout=60,
    )
    if create.returncode != 0:
        err = (create.stderr or create.stdout).strip()
        raise DbBackupError(f"Could not start backup job: {err}", status_code=502)

    wait = run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=complete",
            f"job/{job_name}",
            "-n",
            HCCE_NAMESPACE,
            "--timeout=600s",
        ],
        env=env,
        timeout=620,
    )
    if wait.returncode != 0:
        err = (wait.stderr or wait.stdout).strip()
        raise DbBackupError(
            f"Backup job did not complete: {err or job_name}",
            status_code=502,
        )

    entries = _list_backup_files(instance_id)
    if not entries:
        raise DbBackupError(
            "Backup job finished but no dump file appeared on the volume",
            status_code=502,
        )
    return entries[0]


def list_db_backups(instance_id: str) -> DbBackupsList:
    _ensure_kubeconfig(instance_id)
    try:
        all_entries = _list_backup_files(instance_id)
    except DbBackupError as e:
        if e.status_code == 503:
            return DbBackupsList(available=False, message=str(e))
        raise

    cutoff = date.today() - timedelta(days=RECENT_DAYS - 1)
    recent = [e for e in all_entries if e.backup_date >= cutoff.isoformat()]
    latest = all_entries[0] if all_entries else None
    message = ""
    if not all_entries:
        message = "No backups on the volume yet — the pgsql-backup CronJob runs daily at 04:00 UTC."
    return DbBackupsList(
        available=True,
        message=message,
        keep_days=BACKUP_KEEP_DAYS,
        latest=latest,
        recent=recent,
        all_count=len(all_entries),
    )


def _validate_filename(filename: str) -> str:
    name = Path(filename).name
    if not BACKUP_NAME_RE.match(name):
        raise DbBackupError("Invalid backup filename", status_code=400)
    return name


def resolve_backup(
    instance_id: str,
    *,
    latest: bool = False,
    backup_date: str | None = None,
    filename: str | None = None,
) -> DbBackupEntry:
    entries = _list_backup_files(instance_id)
    if not entries:
        raise DbBackupError("No database backups found on the cluster yet", status_code=404)

    if filename:
        name = _validate_filename(filename)
        for entry in entries:
            if entry.filename == name:
                return entry
        raise DbBackupError(f"Backup not found: {name}", status_code=404)

    if backup_date:
        day = backup_date.strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            raise DbBackupError("Date must be YYYY-MM-DD", status_code=400)
        matches = [e for e in entries if e.backup_date == day]
        if not matches:
            raise DbBackupError(f"No backup for {day}", status_code=404)
        return matches[0]

    if latest:
        return entries[0]

    raise DbBackupError("Specify latest, date, or filename", status_code=400)


def copy_backup_to_file(instance_id: str, entry: DbBackupEntry) -> Path:
    env = kube_env(instance_id)
    out_dir = instance_dir(instance_id) / "tmp" / "db-backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    local_path = out_dir / entry.filename
    remote = f"{BACKUP_DIR}/{entry.filename}"

    with _reader_pod(instance_id):
        cp = run_cmd(
            [
                "kubectl",
                "cp",
                f"{HCCE_NAMESPACE}/{READER_POD}:{remote}",
                str(local_path),
            ],
            env=env,
            timeout=600,
        )
    if cp.returncode != 0 or not local_path.exists():
        err = (cp.stderr or cp.stdout).strip()
        local_path.unlink(missing_ok=True)
        raise DbBackupError(f"Could not download backup: {err}", status_code=502)
    return local_path


def fetch_backup_for_download(
    instance_id: str,
    *,
    latest: bool = False,
    backup_date: str | None = None,
    filename: str | None = None,
) -> tuple[DbBackupEntry, Path]:
    _ensure_kubeconfig(instance_id)
    entry = resolve_backup(instance_id, latest=latest, backup_date=backup_date, filename=filename)
    path = copy_backup_to_file(instance_id, entry)
    return entry, path
