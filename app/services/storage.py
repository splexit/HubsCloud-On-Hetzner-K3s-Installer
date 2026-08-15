from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR, instance_dir
from schemas.models import InstanceSpec, InstanceStatus, InstanceSummary, VolumeInventory
from services.providers.registry import get_cloud_provider
from services.secrets import secrets_available_for_reattach


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_spec(instance_id: str) -> InstanceSpec:
    path = instance_dir(instance_id) / "spec.json"
    return InstanceSpec.model_validate_json(path.read_text())


def save_spec(spec: InstanceSpec) -> None:
    inst = instance_dir(spec.id)
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "spec.json").write_text(spec.model_dump_json(indent=2))


def load_secrets(instance_id: str) -> dict:
    path = instance_dir(instance_id) / "secrets.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_secrets(instance_id: str, secrets: dict) -> None:
    inst = instance_dir(instance_id)
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "secrets.json").write_text(json.dumps(secrets, indent=2))


def load_status(instance_id: str) -> InstanceStatus:
    path = instance_dir(instance_id) / "status.json"
    if not path.exists():
        return InstanceStatus()
    return InstanceStatus.model_validate_json(path.read_text())


def save_status(instance_id: str, status: InstanceStatus) -> None:
    status.updated_at = _now()
    inst = instance_dir(instance_id)
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "status.json").write_text(status.model_dump_json(indent=2))


def append_log(instance_id: str, message: str) -> None:
    inst = instance_dir(instance_id)
    logs_dir = inst / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "pipeline.log"
    line = f"[{_now()}] {message}\n"
    with log_path.open("a") as f:
        f.write(line)


def _saved_volumes_count(instance_id: str) -> int:
    inv = load_volumes_inventory(instance_id)
    if not inv:
        return 0
    return sum(1 for v in inv.volumes if v.hetzner_volume_id)


def _can_recreate_instance(
    spec: InstanceSpec,
    status: InstanceStatus,
    *,
    infrastructure_active: bool,
) -> bool:
    if status.state == "running":
        return False
    if infrastructure_active:
        return False
    if not spec.hub_domain or not spec.hetzner_api_token:
        return False
    if not spec.ssh_key_generated:
        return False
    if secrets_available_for_reattach(spec.id) and _saved_volumes_count(spec.id) > 0:
        return True
    if status.phase == "idle" and status.state == "pending":
        return True
    if status.state == "failed" and status.phase not in ("destroy", "firewall"):
        return True
    if status.state == "succeeded":
        return True
    return False


def _compute_display_status(
    spec: InstanceSpec,
    status: InstanceStatus,
    *,
    infrastructure_active: bool,
    can_recreate: bool,
) -> str:
    if status.state == "running":
        return "destroying" if status.phase == "destroy" else "provisioning"
    if status.state == "failed":
        return "failed"
    if infrastructure_active:
        return "running"
    if can_recreate and _saved_volumes_count(spec.id) > 0:
        return "ready_to_recreate"
    if can_recreate:
        return "stopped"
    if not spec.hub_domain or not spec.ssh_key_generated:
        return "setup_incomplete"
    if status.state == "succeeded":
        return "stopped"
    return "stopped"


def _hetzner_infrastructure_active(spec: InstanceSpec) -> bool:
    if not spec.hetzner_api_token:
        return False
    try:
        audit = get_cloud_provider(spec).audit_cluster(spec.hetzner_api_token)
        return bool(audit.get("servers"))
    except Exception:
        return False


def delete_instance_data(instance_id: str) -> None:
    path = instance_dir(instance_id)
    if not path.exists():
        return
    shutil.rmtree(path)


def list_instances() -> list[InstanceSummary]:
    base = DATA_DIR / "instances"
    if not base.exists():
        return []
    results: list[InstanceSummary] = []
    for path in sorted(base.iterdir(), key=lambda p: p.name):
        if not path.is_dir():
            continue
        spec_path = path / "spec.json"
        if not spec_path.exists():
            continue
        spec = InstanceSpec.model_validate_json(spec_path.read_text())
        status = load_status(spec.id)
        infrastructure_active = _hetzner_infrastructure_active(spec)

        reattach_eligible = False
        if spec.hetzner_api_token:
            try:
                from services.volume_inventory import build_volumes_context

                reattach_eligible = build_volumes_context(spec).reattach_eligible
            except Exception:
                pass

        can_recreate = _can_recreate_instance(
            spec, status, infrastructure_active=infrastructure_active
        )
        display_status = _compute_display_status(
            spec,
            status,
            infrastructure_active=infrastructure_active,
            can_recreate=can_recreate,
        )

        results.append(
            InstanceSummary(
                id=spec.id,
                hub_domain=spec.hub_domain or "(no domain yet)",
                location=spec.location or "",
                state=status.state,
                phase=status.phase,
                message=status.message or "",
                created_at=spec.created_at,
                secrets_available=secrets_available_for_reattach(spec.id),
                saved_volumes_count=_saved_volumes_count(spec.id),
                reattach_eligible=reattach_eligible,
                can_recreate=can_recreate,
                infrastructure_active=infrastructure_active,
                display_status=display_status,
            )
        )
    results.sort(key=lambda s: s.created_at, reverse=True)
    return results


def save_deployment_info(instance_id: str, info: dict) -> None:
    path = instance_dir(instance_id) / "deployment_info.json"
    path.write_text(json.dumps(info, indent=2))


def load_volumes_inventory(instance_id: str) -> VolumeInventory | None:
    path = instance_dir(instance_id) / "volumes-inventory.json"
    if not path.exists():
        return None
    return VolumeInventory.model_validate_json(path.read_text())


def save_volumes_inventory(instance_id: str, inventory: VolumeInventory) -> None:
    inst = instance_dir(instance_id)
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "volumes-inventory.json").write_text(inventory.model_dump_json(indent=2))
