from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from config import instance_dir
from pipeline.core import PipelineError, kube_env, run_cmd
from schemas.models import HCCE_NAMESPACE
from services.storage import append_log, load_secrets

RESTORE_POD = "hcce-db-restore"
PGSQL_DEPLOYMENT = "pgsql"


class DbRestoreError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _wait_pgsql_ready(instance_id: str, timeout: int = 300) -> None:
    env = kube_env(instance_id)
    result = run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=ready",
            "pod",
            "-l",
            "app=pgsql",
            "-n",
            HCCE_NAMESPACE,
            f"--timeout={timeout}s",
        ],
        env=env,
        timeout=timeout + 30,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        raise DbRestoreError(f"PostgreSQL not ready for restore: {err}", status_code=502)


def _snapshot_deployment_replicas(instance_id: str) -> dict[str, int]:
    env = kube_env(instance_id)
    result = run_cmd(
        ["kubectl", "get", "deploy", "-n", HCCE_NAMESPACE, "-o", "json"],
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        return {}
    out: dict[str, int] = {}
    for item in json.loads(result.stdout).get("items", []):
        name = item.get("metadata", {}).get("name", "")
        replicas = (item.get("spec") or {}).get("replicas", 1)
        if name:
            out[name] = int(replicas or 0)
    return out


def _set_deployment_replicas(instance_id: str, replicas: dict[str, int]) -> None:
    env = kube_env(instance_id)
    for name, count in replicas.items():
        run_cmd(
            ["kubectl", "scale", "deployment", name, f"--replicas={count}", "-n", HCCE_NAMESPACE],
            env=env,
            timeout=60,
        )


def _scale_apps_for_restore(instance_id: str) -> dict[str, int]:
    original = _snapshot_deployment_replicas(instance_id)
    paused = {name: (0 if name != PGSQL_DEPLOYMENT else original.get(name, 1)) for name in original}
    _set_deployment_replicas(instance_id, paused)
    deadline = time.time() + 120
    env = kube_env(instance_id)
    while time.time() < deadline:
        result = run_cmd(
            ["kubectl", "get", "pods", "-n", HCCE_NAMESPACE, "-o", "json"],
            env=env,
            timeout=30,
        )
        if result.returncode != 0:
            break
        busy = False
        for pod in json.loads(result.stdout).get("items", []):
            name = pod.get("metadata", {}).get("name", "")
            labels = (pod.get("metadata") or {}).get("labels") or {}
            if labels.get("app") == "pgsql":
                continue
            phase = (pod.get("status") or {}).get("phase", "")
            if phase in ("Running", "Pending", "Terminating"):
                busy = True
                break
        if not busy:
            break
        time.sleep(3)
    return original


def _delete_restore_pod(instance_id: str) -> None:
    env = kube_env(instance_id)
    run_cmd(
        ["kubectl", "delete", "pod", RESTORE_POD, "-n", HCCE_NAMESPACE, "--ignore-not-found", "--wait=false"],
        env=env,
        timeout=60,
    )


def _create_restore_pod(instance_id: str) -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": RESTORE_POD,
            "namespace": HCCE_NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "hubs-installer",
                "app": "hcce-db-restore",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "restore",
                    "image": "ubuntu:22.04",
                    "command": ["sleep", "3600"],
                    "env": [
                        {
                            "name": "PGPASSWORD",
                            "valueFrom": {
                                "secretKeyRef": {"name": "configs", "key": "DB_PASS"},
                            },
                        }
                    ],
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
            raise DbRestoreError(
                f"Could not start restore pod: {(apply.stderr or apply.stdout).strip()}",
                status_code=502,
            )
    finally:
        Path(manifest_path).unlink(missing_ok=True)

    deadline = time.time() + 90
    while time.time() < deadline:
        phase = run_cmd(
            ["kubectl", "get", "pod", RESTORE_POD, "-n", HCCE_NAMESPACE, "-o", "jsonpath={.status.phase}"],
            env=env,
            timeout=30,
        )
        if (phase.stdout or "").strip() == "Running":
            return
        time.sleep(2)
    raise DbRestoreError("Timed out waiting for database restore pod", status_code=504)


def restore_database_from_file(instance_id: str, dump_path: Path) -> None:
    if not dump_path.exists():
        raise DbRestoreError(f"Database dump not found: {dump_path.name}", status_code=404)
    if not load_secrets(instance_id):
        raise DbRestoreError(
            "secrets.json missing — restore bundle secrets must be present for this instance",
            status_code=400,
        )

    append_log(instance_id, f"Database restore starting — {dump_path.name}")
    _wait_pgsql_ready(instance_id)
    original_replicas = _scale_apps_for_restore(instance_id)
    env = kube_env(instance_id)
    remote = f"/tmp/{dump_path.name}"

    try:
        _delete_restore_pod(instance_id)
        _create_restore_pod(instance_id)
        cp = run_cmd(
            ["kubectl", "cp", str(dump_path), f"{HCCE_NAMESPACE}/{RESTORE_POD}:{remote}"],
            env=env,
            timeout=600,
        )
        if cp.returncode != 0:
            raise DbRestoreError(
                f"Could not upload dump to cluster: {(cp.stderr or cp.stdout).strip()}",
                status_code=502,
            )

        script = (
            "set -e; export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && apt-get install -y -qq postgresql-client gzip; "
            f"gunzip -c {remote} | psql -v ON_ERROR_STOP=1 -h pgsql -U postgres -d retdb; "
            f"rm -f {remote}"
        )
        exec_result = run_cmd(
            ["kubectl", "exec", RESTORE_POD, "-n", HCCE_NAMESPACE, "--", "sh", "-c", script],
            env=env,
            timeout=1800,
        )
        if exec_result.returncode != 0:
            err = (exec_result.stderr or exec_result.stdout).strip()
            raise PipelineError(f"Database restore failed: {err}")
        append_log(instance_id, f"Database restore completed — {dump_path.name}")
    finally:
        _delete_restore_pod(instance_id)
        if original_replicas:
            _set_deployment_replicas(instance_id, original_replicas)
