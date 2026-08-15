from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import instance_dir, installer_readme_path
from pipeline.runner import (
    build_debug_bundle,
    build_secrets_zip,
    destroy_cluster,
    get_health,
    get_ingress_load_balancer_info,
    get_resources,
    get_volumes_context,
    harden_firewall,
    mark_pipeline_failed,
    refresh_deployment_lb_ip,
    run_pipeline,
    sync_volume_inventory,
    validate_hetzner_token,
)
from pipeline.cluster_repair import repair_cluster_join
from schemas.models import (
    CLUSTER_SIZE_RECOMMENDATIONS,
    AssetsBackupEntry,
    AssetsBackupsList,
    ClusterJoinStatus,
    DbBackupsList,
    DbBackupEntry,
    HealthSummary,
    HetznerAudit,
    InstanceSpec,
    InstanceStatus,
    InstanceSummary,
    RestoreBundleManifest,
    RestoreBundleStatus,
    VolumesContext,
)
from services.assets_backups import AssetsBackupError, list_assets_backups, resolve_assets_backup, run_assets_backup_now
from services.db_backups import DbBackupError, fetch_backup_for_download, list_db_backups, run_db_backup_now
from services.restore_bundle import build_restore_bundle_zip, create_restore_bundle
from services.restore_bundle_paths import has_restore_bundle, load_restore_bundle_manifest
from services.cluster_join import get_cluster_join_status
from services.hetzner import audit_hetzner_cluster
from services.hetzner import (
    fetch_locations,
    fetch_server_types_for_location,
    is_private_ip,
    validate_server_types,
)
from renderers import render_all
from services.secrets import generate_secrets, write_ssh_keypair
from services.storage import (
    append_log,
    delete_instance_data,
    list_instances,
    load_secrets,
    load_spec,
    load_status,
    save_secrets,
    save_spec,
    save_status,
)

router = APIRouter(prefix="/api")


class SpecUpdate(BaseModel):
    spec: dict


class HardenFirewallBody(BaseModel):
    allow_ssh: bool = True


class DestroyBody(BaseModel):
    confirm: bool = False
    save_restore_bundle: bool = False
    backup_latest: bool = True
    backup_date: str | None = None
    backup_filename: str | None = None


class RestoreBundleCreateBody(BaseModel):
    latest: bool = True
    backup_date: str | None = None
    backup_filename: str | None = None


class DeleteInstanceBody(BaseModel):
    confirm: bool = False


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/docs/installer")
def installer_docs():
    readme = installer_readme_path()
    return {
        "title": "Hubs Installer",
        "markdown": readme.read_text(encoding="utf-8"),
        "source": str(readme),
    }


@router.get("/instances", response_model=list[InstanceSummary])
def get_instances():
    return list_instances()


@router.post("/instances")
def create_instance():
    spec = InstanceSpec()
    save_spec(spec)
    save_status(spec.id, InstanceStatus())
    return spec


@router.post("/instances/{instance_id}/delete-config")
def delete_instance_config(instance_id: str, body: DeleteInstanceBody):
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to delete this saved configuration")
    try:
        spec = load_spec(instance_id)
    except Exception:
        raise HTTPException(404, "Instance not found") from None

    status = load_status(instance_id)
    if status.state == "running":
        raise HTTPException(409, "Cannot delete while provisioning or destroy is running")

    warnings: list[str] = []
    if spec.hetzner_api_token:
        try:
            audit = audit_hetzner_cluster(spec.hetzner_api_token)
            if audit.get("servers"):
                raise HTTPException(
                    409,
                    "Hetzner servers still exist for this configuration — "
                    "run Destroy cluster infrastructure on the progress page first",
                )
            if audit.get("has_billable_leftovers"):
                vol_count = len(audit.get("volumes") or [])
                lb_count = len(audit.get("load_balancers") or [])
                parts = []
                if vol_count:
                    parts.append(f"{vol_count} block volume(s)")
                if lb_count:
                    parts.append(f"{lb_count} load balancer(s)")
                if parts:
                    warnings.append(
                        f"Hetzner still billing {' and '.join(parts)} — deleting this config removes "
                        "volume ID mapping from ./data (keep your secrets ZIP if you may recreate later)"
                    )
        except HTTPException:
            raise
        except Exception as e:
            warnings.append(f"Could not verify Hetzner state: {e}")

    delete_instance_data(instance_id)
    return {"deleted": True, "id": instance_id, "warnings": warnings}


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str):
    spec = load_spec(instance_id)
    status = load_status(instance_id)
    safe = spec.model_dump()
    safe["hetzner_api_token"] = "***" if safe.get("hetzner_api_token") else ""
    safe["smtp_password"] = "***" if safe.get("smtp_password") else ""
    deployment_info = None
    info_path = instance_dir(instance_id) / "deployment_info.json"
    if info_path.exists():
        import json

        deployment_info = json.loads(info_path.read_text())
        needs_lb_refresh = (
            deployment_info.get("dns_pending")
            or not deployment_info.get("lb_ip")
            or is_private_ip(deployment_info.get("lb_ip", ""))
            or any(
                is_private_ip(r.get("target", ""))
                for r in deployment_info.get("dns_records", [])
            )
        )
        if needs_lb_refresh:
            refreshed = refresh_deployment_lb_ip(instance_id)
            if refreshed:
                deployment_info = refreshed
    return {"spec": safe, "status": status, "deployment_info": deployment_info}


@router.get("/instances/{instance_id}/volumes")
def get_instance_volumes(instance_id: str) -> VolumesContext:
    spec = load_spec(instance_id)
    return VolumesContext.model_validate(get_volumes_context(instance_id))


@router.post("/instances/{instance_id}/sync-volumes")
def sync_instance_volumes(instance_id: str) -> VolumesContext:
    return VolumesContext.model_validate(sync_volume_inventory(instance_id))


@router.put("/instances/{instance_id}/spec")
def update_spec(instance_id: str, body: SpecUpdate):
    existing = load_spec(instance_id)
    merged = existing.model_dump()
    incoming = dict(body.spec)
    # SSH keys are only set via POST /ssh/generate
    incoming.pop("ssh_public_key", None)
    incoming.pop("ssh_private_key_pem", None)
    incoming.pop("ssh_key_generated", None)
    merged.update(incoming)
    merged["id"] = instance_id
    spec = InstanceSpec.model_validate(merged)
    save_spec(spec)
    return spec


@router.post("/instances/{instance_id}/validate-hetzner")
def validate_token(instance_id: str):
    spec = load_spec(instance_id)
    ok, msg = validate_hetzner_token(spec.hetzner_api_token)
    return {"ok": ok, "message": msg}


@router.get("/instances/{instance_id}/hetzner/locations")
def hetzner_locations(instance_id: str):
    spec = load_spec(instance_id)
    if not spec.hetzner_api_token:
        raise HTTPException(400, "Hetzner API token required")
    try:
        return {"locations": fetch_locations(spec.hetzner_api_token)}
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/instances/{instance_id}/hetzner/server-types")
def hetzner_server_types(instance_id: str, location: str):
    spec = load_spec(instance_id)
    if not spec.hetzner_api_token:
        raise HTTPException(400, "Hetzner API token required")
    if not (location or "").strip():
        raise HTTPException(400, "location query parameter required")
    try:
        types = fetch_server_types_for_location(spec.hetzner_api_token, location.strip())
        return {
            "location": location.strip(),
            "server_types": [t.to_dict() for t in types if t.available and not t.deprecated],
        }
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/instances/{instance_id}/hetzner/cluster-size-recommendations")
def hetzner_cluster_size_recommendations(instance_id: str):
    load_spec(instance_id)  # ensure instance exists
    return {"recommendations": CLUSTER_SIZE_RECOMMENDATIONS}


@router.get("/instances/{instance_id}/hetzner/presets")
def hetzner_presets_deprecated(instance_id: str, location: str):
    """Deprecated — kept so older UI builds do not 404."""
    load_spec(instance_id)
    return {"location": location, "presets": {}, "recommendations": CLUSTER_SIZE_RECOMMENDATIONS}


@router.get("/instances/{instance_id}/hetzner/load-balancer")
def hetzner_load_balancer(instance_id: str):
    spec = load_spec(instance_id)
    if not spec.hetzner_api_token:
        raise HTTPException(400, "Hetzner API token required")
    try:
        return get_ingress_load_balancer_info(instance_id)
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.post("/instances/{instance_id}/refresh-dns")
def refresh_dns(instance_id: str):
    refreshed = refresh_deployment_lb_ip(instance_id)
    if not refreshed:
        raise HTTPException(404, "Deployment info not found")
    return refreshed


@router.post("/instances/{instance_id}/validate-server-types")
def validate_server_types_endpoint(instance_id: str):
    spec = load_spec(instance_id)
    types = spec.resolved_server_types()
    ok, msg, invalid = validate_server_types(
        spec.hetzner_api_token,
        spec.location,
        types["master_server_type"],
        types["web_server_type"],
        types["webrtc_server_type"],
    )
    return {
        "ok": ok,
        "message": msg,
        "invalid": invalid,
        "resolved": types,
    }


@router.post("/instances/{instance_id}/retry")
def retry_cluster(instance_id: str, background_tasks: BackgroundTasks):
    status = load_status(instance_id)
    if status.state == "running":
        raise HTTPException(409, "Pipeline already running")
    if status.state != "failed":
        raise HTTPException(400, "Retry is only available after a failed run")
    if status.intervention == "cluster_join":
        from services.cluster_join import mark_recreate_workers_on_apply

        mark_recreate_workers_on_apply(instance_id)
        append_log(
            instance_id,
            "Retry after cluster join failure — worker VMs will be recreated for fresh cloud-init",
        )
    save_status(
        instance_id,
        InstanceStatus(phase="validate", step=1, state="running", message="Retrying provisioning"),
    )
    background_tasks.add_task(_run_async, instance_id)
    return {"started": True}


@router.post("/instances/{instance_id}/ssh/generate")
def generate_ssh(instance_id: str):
    inst = instance_dir(instance_id)
    public_key, private_key = write_ssh_keypair(inst)
    spec = load_spec(instance_id)
    spec.ssh_public_key = public_key
    spec.ssh_private_key_pem = private_key
    spec.ssh_key_generated = True
    save_spec(spec)
    return {"public_key": public_key, "generated": True}


@router.post("/instances/{instance_id}/dry-run")
def dry_run(instance_id: str):
    spec = load_spec(instance_id)
    secrets = generate_secrets(spec.hub_domain)
    save_secrets(instance_id, secrets)
    inst = instance_dir(instance_id)
    ssh_dir = inst / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    if spec.ssh_public_key:
        (ssh_dir / "id_ed25519.pub").write_text(spec.ssh_public_key.strip() + "\n")
    render_all(spec, secrets)
    return {"ok": True, "rendered": str(inst / "rendered")}


@router.post("/instances/{instance_id}/create")
def create_cluster(instance_id: str, background_tasks: BackgroundTasks):
    status = load_status(instance_id)
    if status.state == "running":
        raise HTTPException(409, "Pipeline already running")
    spec = load_spec(instance_id)
    if not spec.ssh_key_generated:
        raise HTTPException(400, "Generate an SSH key pair in the wizard first")
    types = spec.resolved_server_types()
    ok, msg, _ = validate_server_types(
        spec.hetzner_api_token,
        spec.location,
        types["master_server_type"],
        types["web_server_type"],
        types["webrtc_server_type"],
    )
    if not ok:
        raise HTTPException(400, f"Server types invalid: {msg}")
    save_status(instance_id, InstanceStatus(phase="validate", step=1, state="running", message="Starting"))
    background_tasks.add_task(_run_async, instance_id)
    return {"started": True}


async def _run_async(instance_id: str):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_pipeline, instance_id)
    except Exception as e:
        mark_pipeline_failed(instance_id, str(e))
        append_log(instance_id, f"Background pipeline exited with error: {e}")
        raise


async def _harden_firewall_async(instance_id: str, allow_ssh: bool):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, harden_firewall, instance_id, allow_ssh)
    except Exception as e:
        append_log(instance_id, f"Firewall hardening exited with error: {e}")
        status = load_status(instance_id)
        if status.state == "running" and status.phase == "firewall":
            err = str(e).strip() or "Firewall hardening failed"
            save_status(
                instance_id,
                InstanceStatus(
                    phase="firewall",
                    step=11,
                    state="failed",
                    message="Firewall hardening failed",
                    error=err,
                    intervention="firewall",
                ),
            )
        raise


async def _repair_cluster_join_async(instance_id: str):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, repair_cluster_join, instance_id)
    except Exception as e:
        append_log(instance_id, f"Cluster join repair exited with error: {e}")
        raise


async def _destroy_async(
    instance_id: str,
    *,
    save_restore_bundle: bool = False,
    backup_latest: bool = True,
    backup_date: str | None = None,
    backup_filename: str | None = None,
):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: destroy_cluster(
                instance_id,
                save_restore_bundle=save_restore_bundle,
                backup_latest=backup_latest,
                backup_date=backup_date,
                backup_filename=backup_filename,
            ),
        )
    except Exception as e:
        append_log(instance_id, f"Destroy exited with error: {e}")
        status = load_status(instance_id)
        if status.phase == "destroy" and status.state == "running":
            err = str(e).strip() or "Destroy failed"
            save_status(
                instance_id,
                InstanceStatus(
                    phase="destroy",
                    step=0,
                    state="failed",
                    message="Destroy failed",
                    error=err,
                    intervention="destroy",
                ),
            )


@router.post("/instances/{instance_id}/destroy")
def destroy_cluster_endpoint(instance_id: str, body: DestroyBody, background_tasks: BackgroundTasks):
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to destroy all Hetzner resources")
    status = load_status(instance_id)
    if status.phase == "destroy" and status.state == "running":
        from datetime import datetime, timezone

        try:
            updated = datetime.fromisoformat(status.updated_at.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
        except (ValueError, TypeError):
            age_seconds = 9999.0
        if age_seconds < 120:
            raise HTTPException(409, "Destroy already running")
        append_log(instance_id, "Retrying destroy (previous run did not finish)")
    if status.state == "running" and status.phase in ("validate", "render", "terraform"):
        raise HTTPException(409, "Wait for Terraform to finish (or fail) before destroying")
    if status.state == "running" and status.phase != "destroy":
        append_log(instance_id, "Destroy requested — stopping active provisioning")
        save_status(
            instance_id,
            InstanceStatus(
                phase=status.phase,
                step=status.step,
                state="failed",
                message="Stopped for destroy",
                error="Stopped for destroy",
            ),
        )
    save_status(
        instance_id,
        InstanceStatus(phase="destroy", step=0, state="running", message="Destroying Hetzner resources"),
    )
    background_tasks.add_task(
        _destroy_async,
        instance_id,
        save_restore_bundle=body.save_restore_bundle,
        backup_latest=body.backup_latest,
        backup_date=body.backup_date,
        backup_filename=body.backup_filename,
    )
    return {"started": True}


@router.post("/instances/{instance_id}/harden-firewall")
def harden_firewall_endpoint(instance_id: str, body: HardenFirewallBody, background_tasks: BackgroundTasks):
    status = load_status(instance_id)
    if status.phase == "firewall" and status.state == "running":
        raise HTTPException(409, "Firewall hardening already running")
    if status.state == "running" and status.phase != "firewall":
        raise HTTPException(409, "Pipeline already running")
    if status.state != "succeeded" and status.phase != "firewall":
        raise HTTPException(400, "Cluster must finish provisioning before firewall hardening")
    spec = load_spec(instance_id)
    if spec.firewall_hardened:
        raise HTTPException(409, "Firewall already hardened")
    background_tasks.add_task(_harden_firewall_async, instance_id, body.allow_ssh)
    return {"started": True}


@router.get("/instances/{instance_id}/hetzner/audit")
def hetzner_audit(instance_id: str) -> HetznerAudit:
    spec = load_spec(instance_id)
    if not spec.hetzner_api_token:
        raise HTTPException(400, "Hetzner API token required")
    try:
        return HetznerAudit.model_validate(audit_hetzner_cluster(spec.hetzner_api_token))
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/instances/{instance_id}/cluster-join-status")
def cluster_join_status(instance_id: str) -> ClusterJoinStatus:
    return get_cluster_join_status(instance_id)


@router.post("/instances/{instance_id}/repair-cluster-join")
def repair_cluster_join_endpoint(instance_id: str, background_tasks: BackgroundTasks):
    status = load_status(instance_id)
    cluster_stuck = status.phase == "cluster" and status.state == "running"
    cluster_failed = status.state == "failed" and status.intervention == "cluster_join"
    if not cluster_stuck and not cluster_failed:
        raise HTTPException(
            400,
            "Repair is available while waiting for cluster join or after a cluster join failure",
        )
    background_tasks.add_task(_repair_cluster_join_async, instance_id)
    return {"started": True}


@router.post("/instances/{instance_id}/abort")
def abort_provisioning(instance_id: str):
    status = load_status(instance_id)
    if status.state != "running":
        raise HTTPException(400, "Nothing to abort — provisioning is not running")
    save_status(
        instance_id,
        InstanceStatus(
            phase=status.phase or "cluster",
            step=status.step or 4,
            state="failed",
            message="Stopped by user — cluster join incomplete",
            error="Stopped by user — cluster join incomplete",
            intervention="cluster_join",
        ),
    )
    append_log(instance_id, "Provisioning aborted by user")
    return {"aborted": True}


@router.get("/instances/{instance_id}/status")
def get_status(instance_id: str):
    return load_status(instance_id)


@router.get("/instances/{instance_id}/resources")
def resources(instance_id: str):
    return get_resources(instance_id)


@router.get("/instances/{instance_id}/health")
def health(instance_id: str) -> HealthSummary:
    return HealthSummary.model_validate(get_health(instance_id))


@router.get("/instances/{instance_id}/logs")
def logs(instance_id: str, lines: int = 200):
    log_path = instance_dir(instance_id) / "logs" / "pipeline.log"
    if not log_path.exists():
        return {"lines": []}
    content = log_path.read_text().splitlines()
    return {"lines": content[-lines:]}


@router.get("/instances/{instance_id}/debug-bundle.zip")
def debug_bundle(instance_id: str):
    data = build_debug_bundle(instance_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="hubs-debug-{instance_id[:8]}.zip"'},
    )


@router.get("/instances/{instance_id}/secrets-bundle.zip")
def secrets_zip(instance_id: str, progress_url: str | None = None):
    url = (progress_url or "").strip()
    if url and len(url) > 512:
        raise HTTPException(400, "progress_url too long")
    data = build_secrets_zip(instance_id, progress_url=url or None)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="hubs-secrets-{instance_id[:8]}.zip"'},
    )


@router.get("/instances/{instance_id}/db-backups", response_model=DbBackupsList)
def db_backups_list(instance_id: str) -> DbBackupsList:
    try:
        return list_db_backups(instance_id)
    except DbBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.post("/instances/{instance_id}/db-backups/run", response_model=DbBackupEntry)
def db_backup_run(instance_id: str) -> DbBackupEntry:
    try:
        return run_db_backup_now(instance_id)
    except DbBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.get("/instances/{instance_id}/assets-backups", response_model=AssetsBackupsList)
def assets_backups_list(instance_id: str) -> AssetsBackupsList:
    return list_assets_backups(instance_id)


@router.post("/instances/{instance_id}/assets-backups/run", response_model=AssetsBackupEntry)
def assets_backup_run(instance_id: str) -> AssetsBackupEntry:
    try:
        return run_assets_backup_now(instance_id)
    except AssetsBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.get("/instances/{instance_id}/assets-backups/download")
def assets_backup_download(
    instance_id: str,
    latest: bool = Query(False),
    filename: str | None = Query(None),
):
    try:
        entry, path = resolve_assets_backup(instance_id, latest=latest, filename=filename)
    except AssetsBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e
    return FileResponse(
        path,
        filename=entry.filename,
        media_type="application/gzip",
    )


@router.get("/instances/{instance_id}/db-backups/download")
def db_backup_download(
    instance_id: str,
    background_tasks: BackgroundTasks,
    latest: bool = Query(False),
    date: str | None = Query(None, alias="date"),
    filename: str | None = Query(None),
):
    try:
        entry, path = fetch_backup_for_download(
            instance_id,
            latest=latest,
            backup_date=date,
            filename=filename,
        )
    except DbBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e

    background_tasks.add_task(path.unlink, missing_ok=True)
    return FileResponse(
        path,
        filename=entry.filename,
        media_type="application/gzip",
    )


@router.get("/instances/{instance_id}/restore-bundle", response_model=RestoreBundleStatus)
def restore_bundle_status(instance_id: str) -> RestoreBundleStatus:
    manifest = load_restore_bundle_manifest(instance_id)
    if not manifest or not has_restore_bundle(instance_id):
        return RestoreBundleStatus(
            present=False,
            message="No local restore bundle — save one before destroy or from database backups.",
        )
    return RestoreBundleStatus(present=True, manifest=manifest)


@router.post("/instances/{instance_id}/restore-bundle", response_model=RestoreBundleManifest)
def restore_bundle_create(instance_id: str, body: RestoreBundleCreateBody) -> RestoreBundleManifest:
    try:
        return create_restore_bundle(
            instance_id,
            latest=body.latest,
            backup_date=body.backup_date,
            backup_filename=body.backup_filename,
        )
    except DbBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.get("/instances/{instance_id}/restore-bundle.zip")
def restore_bundle_download(instance_id: str):
    try:
        data = build_restore_bundle_zip(instance_id)
    except DbBackupError as e:
        raise HTTPException(e.status_code, str(e)) from e
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="hubs-restore-bundle-{instance_id[:8]}.zip"'},
    )


@router.get("/instances/{instance_id}/ssh/private-key")
def download_ssh_key(instance_id: str):
    key = instance_dir(instance_id) / "ssh" / "id_ed25519"
    if not key.exists():
        raise HTTPException(404, "No generated SSH key")
    return FileResponse(key, filename="id_ed25519", media_type="application/octet-stream")
