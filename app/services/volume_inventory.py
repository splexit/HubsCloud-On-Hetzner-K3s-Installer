from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import instance_dir
from pipeline.core import kube_env, run_cmd
from schemas.models import (
    HCCE_NAMESPACE,
    KNOWN_VOLUME_PVCS,
    InstanceSpec,
    VolumeContextEntry,
    VolumeInventory,
    VolumeInventoryEntry,
    VolumesContext,
)
from services.providers.hetzner.api import fetch_volumes, find_persistent_data_volumes
from services.secrets import secrets_available_for_reattach
from services.storage import append_log, load_volumes_inventory, save_volumes_inventory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _size_for_pvc(spec: InstanceSpec, pvc_name: str) -> str:
    meta = KNOWN_VOLUME_PVCS.get(pvc_name, {})
    field = meta.get("size_field", "pgsql_volume_size")
    return str(getattr(spec, field, "10Gi"))


def _role_for_pvc(pvc_name: str) -> str:
    return KNOWN_VOLUME_PVCS.get(pvc_name, {}).get("role", pvc_name)


def _empty_inventory(spec: InstanceSpec) -> VolumeInventory:
    return VolumeInventory(
        updated_at=_now(),
        location=spec.location,
        volumes=[
            VolumeInventoryEntry(
                pvc_name=name,
                namespace=HCCE_NAMESPACE,
                role=_role_for_pvc(name),
                size=_size_for_pvc(spec, name),
                status="unknown",
            )
            for name in KNOWN_VOLUME_PVCS
        ],
    )


def _index_by_pvc(inventory: VolumeInventory) -> dict[str, VolumeInventoryEntry]:
    return {v.pvc_name: v for v in inventory.volumes}


def _index_by_pv_name(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if name:
            out[name] = item
    return out


def _index_pvcs(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if name:
            out[name] = item
    return out


def sync_from_cluster(instance_id: str, spec: InstanceSpec) -> VolumeInventory:
    """Read PVC/PV state from the live cluster and merge into inventory."""
    kc = instance_dir(instance_id) / "kubeconfig"
    existing = load_volumes_inventory(instance_id)
    base = existing or _empty_inventory(spec)
    by_pvc = _index_by_pvc(base)

    for name in KNOWN_VOLUME_PVCS:
        if name not in by_pvc:
            by_pvc[name] = VolumeInventoryEntry(
                pvc_name=name,
                namespace=HCCE_NAMESPACE,
                role=_role_for_pvc(name),
                size=_size_for_pvc(spec, name),
                status="unknown",
            )

    if not kc.exists():
        inventory = VolumeInventory(updated_at=_now(), location=spec.location, volumes=list(by_pvc.values()))
        save_volumes_inventory(instance_id, inventory)
        return inventory

    env = kube_env(instance_id)
    pvc_result = run_cmd(
        ["kubectl", "get", "pvc", "-n", HCCE_NAMESPACE, "-o", "json"],
        env=env,
        timeout=60,
    )
    pv_result = run_cmd(
        ["kubectl", "get", "pv", "-o", "json"],
        env=env,
        timeout=60,
    )
    if pvc_result.returncode != 0:
        inventory = VolumeInventory(updated_at=_now(), location=spec.location, volumes=list(by_pvc.values()))
        save_volumes_inventory(instance_id, inventory)
        return inventory

    pvc_items = json.loads(pvc_result.stdout).get("items", [])
    pv_items = json.loads(pv_result.stdout).get("items", []) if pv_result.returncode == 0 else []
    pv_by_name = _index_by_pv_name(pv_items)
    pvc_by_name = _index_pvcs(pvc_items)

    for pvc_name, meta in KNOWN_VOLUME_PVCS.items():
        entry = by_pvc[pvc_name]
        entry.role = _role_for_pvc(pvc_name)
        entry.size = _size_for_pvc(spec, pvc_name)
        pvc = pvc_by_name.get(pvc_name)
        if not pvc:
            if entry.status not in ("orphaned", "missing"):
                entry.status = "unknown"
            continue

        entry.pvc_uid = pvc.get("metadata", {}).get("uid")
        phase = (pvc.get("status") or {}).get("phase", "").lower()
        pv_name = (pvc.get("spec") or {}).get("volumeName")
        entry.pv_name = pv_name

        if phase == "bound" and pv_name and pv_name in pv_by_name:
            pv = pv_by_name[pv_name]
            csi = (pv.get("spec") or {}).get("csi") or {}
            handle = csi.get("volumeHandle")
            if handle:
                entry.hetzner_volume_id = int(handle)
            entry.hetzner_volume_name = pv_name
            entry.status = "bound"
            if not entry.bound_at:
                entry.bound_at = _now()
        elif phase == "pending":
            entry.status = "pending"
        elif phase:
            entry.status = phase

    inventory = VolumeInventory(updated_at=_now(), location=spec.location, volumes=list(by_pvc.values()))
    save_volumes_inventory(instance_id, inventory)
    return inventory


def mark_inventory_orphaned(instance_id: str, spec: InstanceSpec, hetzner_volume_ids: list[int] | None = None) -> VolumeInventory:
    """After cluster destroy — keep Hetzner IDs, mark entries orphaned."""
    inventory = sync_from_cluster(instance_id, spec)
    by_pvc = _index_by_pvc(inventory)
    hetzner_ids = set(hetzner_volume_ids or [])

    hetzner_volumes: dict[int, dict] = {}
    if spec.hetzner_api_token:
        try:
            for vol in find_persistent_data_volumes(spec.hetzner_api_token):
                hetzner_volumes[int(vol["id"])] = vol
        except Exception:
            pass

    for entry in by_pvc.values():
        if entry.hetzner_volume_id:
            entry.status = "orphaned"
        elif entry.status == "bound":
            entry.status = "orphaned"

    for vol_id, vol in hetzner_volumes.items():
        if hetzner_ids and vol_id not in hetzner_ids:
            continue
        name = (vol.get("name") or "").lower()
        matched = False
        for entry in by_pvc.values():
            if entry.hetzner_volume_id == vol_id:
                entry.status = "orphaned"
                entry.hetzner_volume_name = vol.get("name")
                matched = True
                break
            if entry.pvc_uid and name == f"pvc-{entry.pvc_uid}".lower():
                entry.hetzner_volume_id = vol_id
                entry.hetzner_volume_name = vol.get("name")
                entry.pv_name = vol.get("name")
                entry.status = "orphaned"
                matched = True
                break
        if not matched and name.startswith("pvc-"):
            # Unknown CSI volume — attach to first known slot without an ID if sizes match
            size_gb = int(vol.get("size") or 0)
            for entry in by_pvc.values():
                if entry.hetzner_volume_id:
                    continue
                try:
                    requested = int(str(entry.size).replace("Gi", ""))
                except ValueError:
                    requested = 0
                if requested == size_gb:
                    entry.hetzner_volume_id = vol_id
                    entry.hetzner_volume_name = vol.get("name")
                    entry.status = "orphaned"
                    break

    inventory.updated_at = _now()
    inventory.location = spec.location
    inventory.volumes = list(by_pvc.values())
    save_volumes_inventory(instance_id, inventory)
    return inventory


def hetzner_volume_exists(token: str, volume_id: int) -> tuple[bool, dict | None]:
    try:
        for vol in fetch_volumes(token):
            if int(vol.get("id") or 0) == volume_id:
                return True, vol
    except Exception:
        return False, None
    return False, None


def validate_reattach(spec: InstanceSpec, inventory: VolumeInventory | None = None) -> list[str]:
    if spec.volume_reattach.mode != "reattach_saved":
        return []

    inv = inventory or load_volumes_inventory(spec.id)
    if not inv:
        return ["Volume reattach selected but no saved volume inventory found for this instance"]

    errors: list[str] = []
    by_pvc = _index_by_pvc(inv)
    selected_any = False

    for pvc_name in KNOWN_VOLUME_PVCS:
        entry = by_pvc.get(pvc_name)
        has_id = bool(entry and entry.hetzner_volume_id)
        if not spec.volume_reattach.wants_reattach(pvc_name, has_saved_id=has_id):
            continue
        selected_any = True
        if not entry or not entry.hetzner_volume_id:
            errors.append(f"Reattach requested for {pvc_name} but no Hetzner volume ID is saved")
            continue

        exists, vol = hetzner_volume_exists(spec.hetzner_api_token, entry.hetzner_volume_id)
        if not exists:
            errors.append(
                f"Hetzner volume {entry.hetzner_volume_id} for {pvc_name} not found — "
                "disable reattach for this volume or restore it in Hetzner Console"
            )
            continue

        vol_size = int((vol or {}).get("size") or 0)
        requested = int(_size_for_pvc(spec, pvc_name).replace("Gi", ""))
        if vol_size < requested:
            errors.append(
                f"Hetzner volume {entry.hetzner_volume_id} ({vol_size} Gi) is smaller than "
                f"requested {requested} Gi for {pvc_name}"
            )
            continue

        loc = ((vol or {}).get("location") or {}).get("name", "")
        if spec.location and loc and loc != spec.location:
            errors.append(
                f"Hetzner volume {entry.hetzner_volume_id} is in {loc} but cluster location is {spec.location}"
            )

    if not selected_any:
        errors.append("Reattach mode selected but no volumes are enabled for reattach")

    if selected_any and not secrets_available_for_reattach(spec.id):
        errors.append(
            "Reattach requires saved secrets from the original provision — "
            "use the same instance or import your secrets bundle before recreating"
        )

    return errors


def reattach_entries(spec: InstanceSpec, inventory: VolumeInventory | None = None) -> list[VolumeInventoryEntry]:
    inv = inventory or load_volumes_inventory(spec.id)
    if not inv or spec.volume_reattach.mode != "reattach_saved":
        return []

    by_pvc = _index_by_pvc(inv)
    out: list[VolumeInventoryEntry] = []
    for pvc_name in KNOWN_VOLUME_PVCS:
        entry = by_pvc.get(pvc_name)
        if not entry or not entry.hetzner_volume_id:
            continue
        if spec.volume_reattach.wants_reattach(pvc_name, has_saved_id=True):
            out.append(entry)
    return out


def build_volumes_context(spec: InstanceSpec) -> VolumesContext:
    inventory = load_volumes_inventory(spec.id) or _empty_inventory(spec)
    by_pvc = _index_by_pvc(inventory)

    hetzner_by_id: dict[int, dict] = {}
    if spec.hetzner_api_token:
        try:
            for vol in find_persistent_data_volumes(spec.hetzner_api_token):
                hetzner_by_id[int(vol["id"])] = vol
        except Exception:
            pass

    entries: list[VolumeContextEntry] = []
    any_eligible = False

    for pvc_name in KNOWN_VOLUME_PVCS:
        inv_entry = by_pvc.get(pvc_name)
        hetzner_id = inv_entry.hetzner_volume_id if inv_entry else None
        in_hetzner = bool(hetzner_id and hetzner_id in hetzner_by_id)
        has_saved_id = bool(hetzner_id)
        selected = spec.volume_reattach.wants_reattach(pvc_name, has_saved_id=has_saved_id)
        eligible = has_saved_id and in_hetzner and (inv_entry.status in ("bound", "orphaned", "pending", "unknown"))
        if eligible:
            any_eligible = True

        entries.append(
            VolumeContextEntry(
                pvc_name=pvc_name,
                role=_role_for_pvc(pvc_name),
                size=_size_for_pvc(spec, pvc_name),
                hetzner_volume_id=hetzner_id,
                hetzner_volume_name=(inv_entry.hetzner_volume_name if inv_entry else None),
                status=(inv_entry.status if inv_entry else "unknown"),
                in_hetzner=in_hetzner,
                in_inventory=bool(inv_entry and has_saved_id),
                selected_for_reattach=selected and spec.volume_reattach.mode == "reattach_saved",
                reattach_eligible=eligible,
            )
        )

    return VolumesContext(
        inventory=inventory,
        reattach_eligible=any_eligible,
        secrets_available=secrets_available_for_reattach(spec.id),
        entries=entries,
    )


def wait_for_pvc_binding(instance_id: str, timeout: int = 600) -> None:
    """Poll until known PVCs bind or timeout (backup PVC may stay pending)."""
    deadline = time.time() + timeout
    env = kube_env(instance_id)
    required = ("pgsql-pvc", "ret-pvc")

    while time.time() < deadline:
        result = run_cmd(
            ["kubectl", "get", "pvc", "-n", HCCE_NAMESPACE, "-o", "json"],
            env=env,
            timeout=30,
        )
        if result.returncode != 0:
            time.sleep(10)
            continue
        by_name = _index_pvcs(json.loads(result.stdout).get("items", []))
        if all((by_name.get(n, {}).get("status") or {}).get("phase") == "Bound" for n in required):
            return
        time.sleep(10)

    append_log(instance_id, "Timed out waiting for all core PVCs to bind — saving partial volume inventory")


def snapshot_on_destroy(instance_id: str, spec: InstanceSpec, hetzner_volume_ids: list[int] | None = None) -> None:
    try:
        inv = mark_inventory_orphaned(instance_id, spec, hetzner_volume_ids)
        parts = [
            f"{e.pvc_name}→{e.hetzner_volume_id}"
            for e in inv.volumes
            if e.hetzner_volume_id
        ]
        if parts:
            append_log(instance_id, f"Volume inventory preserved for reattach: {', '.join(parts)}")
        else:
            append_log(instance_id, "Volume inventory updated after destroy (no Hetzner volume IDs recorded)")
    except Exception as e:
        append_log(instance_id, f"Could not snapshot volume inventory on destroy: {e}")
