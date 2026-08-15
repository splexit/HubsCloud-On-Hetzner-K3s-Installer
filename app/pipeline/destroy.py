from __future__ import annotations

from config import instance_dir
from pipeline.core import PipelineError, run_cmd, update_status
from pipeline.errors import format_terraform_error
from schemas.models import InstanceSpec
from services.providers.registry import get_cloud_provider
from services.storage import append_log, load_spec, save_spec


def _clear_local_cluster_artifacts(instance_id: str, spec: InstanceSpec) -> None:
    inst = instance_dir(instance_id)
    for name in ("kubeconfig", "deployment_info.json", "cluster_wait_started"):
        path = inst / name
        if path.exists():
            path.unlink()
    from services.cluster_join import recreate_workers_marker

    marker = recreate_workers_marker(instance_id)
    if marker.exists():
        marker.unlink()
    tf_dir = inst / "terraform"
    for path in tf_dir.glob("terraform.tfstate*"):
        path.unlink()
    spec.firewall_hardened = False
    save_spec(spec)


def _leftover_summary(verify: dict) -> str:
    lb_count = len(verify.get("load_balancers") or [])
    vol_count = len(verify.get("volumes") or [])
    if lb_count == 0 and vol_count == 0:
        return ""
    parts: list[str] = []
    if lb_count:
        parts.append(f"{lb_count} load balancer(s)")
    if vol_count:
        parts.append(f"{vol_count} block volume(s)")
    joined = " and ".join(parts)
    return (
        f"{joined} still in Hetzner and continue billing — intentional "
        "(Kubernetes ingress + database data). Delete manually in Hetzner Console when you no longer need them."
    )


def destroy_cluster(
    instance_id: str,
    *,
    save_restore_bundle: bool = False,
    backup_latest: bool = True,
    backup_date: str | None = None,
    backup_filename: str | None = None,
) -> dict:
    """Terraform destroy of installer-managed infrastructure.

    Kubernetes-created load balancers and CSI block volumes are left in place on purpose
    so persistent data (PostgreSQL, etc.) is not destroyed accidentally.
    """
    spec = load_spec(instance_id)
    if not spec.hetzner_api_token:
        raise PipelineError("Cloud API token required to destroy resources")

    provider = get_cloud_provider(spec)
    token = spec.hetzner_api_token
    tf_dir = instance_dir(instance_id) / "terraform"
    has_state = any(tf_dir.glob("terraform.tfstate*"))
    summary: dict = {
        "terraform_destroyed": False,
        "load_balancers_deleted": [],
        "volumes_deleted": [],
        "billable_leftovers": {"load_balancers": [], "volumes": []},
        "errors": [],
        "restore_bundle_saved": False,
    }

    update_status(instance_id, "destroy", 0, "running", f"Destroying {provider.id} cluster infrastructure")
    append_log(
        instance_id,
        "Destroy started — terraform destroy (servers, network, firewall). "
        "Kubernetes load balancers and CSI volumes are kept on purpose.",
    )

    if save_restore_bundle:
        from services.db_backups import DbBackupError
        from services.restore_bundle import create_restore_bundle

        try:
            manifest = create_restore_bundle(
                instance_id,
                latest=backup_latest,
                backup_date=backup_date,
                backup_filename=backup_filename,
            )
            summary["restore_bundle_saved"] = True
            summary["restore_bundle"] = manifest.model_dump()
            spec.restore_from_bundle.enabled = True
            save_spec(spec)
            append_log(
                instance_id,
                "Restore bundle saved locally — recreate can use this dump without Hetzner volumes",
            )
        except DbBackupError as e:
            err = f"Restore bundle failed: {e}"
            append_log(instance_id, err)
            summary["errors"].append(err)
            update_status(instance_id, "destroy", 0, "failed", err, error=err, intervention="destroy")
            raise PipelineError(err) from e

    inventory: dict = {"cluster_ids": set(), "load_balancers": [], "persistent_volumes": []}
    try:
        inventory = provider.pre_destroy_inventory(token)
        skipped_lbs = len(inventory.get("load_balancers") or [])
        skipped_vols = len(inventory.get("persistent_volumes") or [])
        append_log(
            instance_id,
            f"Found {len(inventory.get('cluster_ids', []))} cluster server(s). "
            f"Will keep {skipped_lbs} load balancer(s) and {skipped_vols} CSI volume(s) if present.",
        )
    except Exception as e:
        msg = f"Could not list cloud resources before destroy: {e}"
        append_log(instance_id, msg)
        summary["errors"].append(msg)

    if has_state:
        try:
            run_cmd(["terraform", "init", "-input=false"], cwd=tf_dir, timeout=600)
            result = run_cmd(
                ["terraform", "destroy", "-auto-approve", "-input=false", "-no-color"],
                cwd=tf_dir,
                timeout=1800,
            )
            append_log(instance_id, result.stdout)
            if result.returncode != 0:
                append_log(instance_id, result.stderr)
                err = format_terraform_error(result.stderr, result.stdout)
                summary["errors"].append(err)
                update_status(instance_id, "destroy", 0, "failed", "Terraform destroy failed", error=err, intervention="destroy")
                raise PipelineError(err)
            summary["terraform_destroyed"] = True
            append_log(instance_id, "Terraform destroy completed")
        except PipelineError:
            raise
        except Exception as e:
            err = str(e).strip() or "Terraform destroy failed"
            summary["errors"].append(err)
            update_status(instance_id, "destroy", 0, "failed", "Terraform destroy failed", error=err, intervention="destroy")
            raise PipelineError(err) from e
    else:
        append_log(instance_id, "No Terraform state on disk — skipping terraform destroy")
        try:
            deleted = provider.force_delete_servers(token)
            if deleted:
                append_log(instance_id, f"Deleted hcce servers via API (no tfstate): {', '.join(deleted)}")
        except Exception as e:
            msg = f"API server cleanup failed: {e}"
            append_log(instance_id, msg)
            summary["errors"].append(msg)

    extras = provider.delete_post_terraform_extras(token, inventory)
    skipped = extras.get("skipped") or {}
    summary["billable_leftovers"]["load_balancers"] = skipped.get("load_balancers") or []
    summary["billable_leftovers"]["volumes"] = skipped.get("volumes") or []
    if skipped.get("load_balancers") or skipped.get("volumes"):
        append_log(
            instance_id,
            "Left Kubernetes-managed resources in Hetzner (not deleted): "
            f"{len(skipped.get('load_balancers') or [])} load balancer(s), "
            f"{len(skipped.get('volumes') or [])} block volume(s)",
        )

    hetzner_vol_ids = [int(v["id"]) for v in (inventory.get("persistent_volumes") or []) if v.get("id")]
    from services.secrets import secrets_available_for_reattach
    from services.volume_inventory import build_volumes_context, snapshot_on_destroy

    snapshot_on_destroy(instance_id, spec, hetzner_vol_ids or None)

    ctx = build_volumes_context(spec)
    if ctx.reattach_eligible and secrets_available_for_reattach(instance_id):
        if spec.volume_reattach.mode != "reattach_saved":
            spec.volume_reattach.mode = "reattach_saved"
            save_spec(spec)
            append_log(
                instance_id,
                "Reattach saved volumes enabled by default — recreate will bind existing Hetzner block volumes",
            )

    _clear_local_cluster_artifacts(instance_id, spec)

    verify = provider.audit_cluster(token)
    summary["hetzner_clean"] = verify["clean"]
    summary["hetzner_issues"] = verify["issues"]
    summary["hetzner_warnings"] = verify.get("warnings") or []
    summary["has_billable_leftovers"] = verify.get("has_billable_leftovers", False)
    summary["billable_leftovers"]["load_balancers"] = verify.get("load_balancers") or summary["billable_leftovers"]["load_balancers"]
    summary["billable_leftovers"]["volumes"] = [{"id": vid} for vid in (verify.get("volumes") or [])]

    for issue in verify.get("issues") or []:
        append_log(instance_id, f"Post-destroy audit (blocking): {issue}")
    for warning in verify.get("warnings") or []:
        append_log(instance_id, f"Post-destroy audit (billable leftover): {warning}")

    leftover_note = _leftover_summary(verify)

    if summary["errors"] or not verify["clean"]:
        err_parts = list(summary["errors"][:2])
        if not verify["clean"]:
            err_parts.append("Terraform-managed resources still present — see activity log")
        update_status(
            instance_id,
            "destroy",
            0,
            "failed",
            "Destroy finished with errors — check activity log",
            error="; ".join(err_parts) or "Destroy incomplete",
            intervention="destroy",
        )
    else:
        message = f"Cluster infrastructure destroyed — ready to provision again"
        if leftover_note:
            message = f"{message}. {leftover_note}"
        update_status(instance_id, "idle", 0, "pending", message)
        append_log(instance_id, f"Destroy complete — {provider.id} cluster infrastructure removed")
        if leftover_note:
            append_log(instance_id, leftover_note)

    return summary
