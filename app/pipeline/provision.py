from __future__ import annotations

from config import instance_dir
from pipeline.cluster_ops import fetch_kubeconfig, harden_cluster_ssh, wait_nodes_ready
from pipeline.core import PipelineError, kube_env, mark_pipeline_failed, run_cmd, update_status
from pipeline.deployment import build_deployment_info, wait_lb_public_ip
from pipeline.spec_validation import (
    reset_stale_terraform_after_console_wipe,
    validate_spec,
    write_ssh_files,
)
from pipeline.terraform_ops import terraform_output, terraform_step
from pipeline.workloads import install_ccm_csi, install_cert_manager, kubectl_apply_dir
from renderers import plan_labels, plan_svclb_labels, render_all, render_platform, render_static_pvs
from services.providers.registry import get_cloud_provider
from services.secrets import generate_secrets
from services.storage import (
    append_log,
    load_secrets,
    load_spec,
    save_deployment_info,
    save_secrets,
)


def run_pipeline(instance_id: str) -> None:
    spec = load_spec(instance_id)
    provider = get_cloud_provider(spec)
    try:
        update_status(instance_id, "validate", 1, "running", "Validating specification")
        validate_spec(spec)
        write_ssh_files(spec)

        update_status(instance_id, "render", 2, "running", "Generating secrets and rendering templates")
        secrets = load_secrets(instance_id) or generate_secrets(spec.hub_domain)
        save_secrets(instance_id, secrets)
        render_all(spec, secrets)

        tf_dir = instance_dir(instance_id) / "terraform"
        reset_stale_terraform_after_console_wipe(instance_id, spec)
        has_tf_state = any(tf_dir.glob("terraform.tfstate*"))
        cloud_audit = provider.audit_cluster(spec.hetzner_api_token)
        if cloud_audit["servers"] and not has_tf_state:
            issues = "; ".join(cloud_audit["issues"][:5])
            raise PipelineError(
                f"Hetzner is not clean — HCCE resources still exist ({issues}). "
                "Run Destroy all Hetzner resources on the progress page before provisioning again."
            )
        if cloud_audit["issues"]:
            append_log(
                instance_id,
                f"Cloud audit before apply: {len(cloud_audit['issues'])} item(s) — {', '.join(cloud_audit['issues'][:3])}",
            )
        else:
            append_log(instance_id, "Cloud audit: no HCCE resources found (clean for fresh provision)")
        update_status(instance_id, "terraform", 3, "running", "Running terraform init/plan/apply")
        terraform_step(instance_id, ["terraform", "init", "-input=false"], tf_dir, "init", timeout=600)
        terraform_step(instance_id, ["terraform", "plan", "-input=false", "-no-color"], tf_dir, "plan", timeout=600)
        apply_cmd = ["terraform", "apply", "-auto-approve", "-input=false", "-no-color"]
        from services.cluster_join import consume_recreate_workers_marker

        if consume_recreate_workers_marker(instance_id):
            append_log(
                instance_id,
                "Recreating worker servers (fresh cloud-init) — previous run left partially configured VMs",
            )
            apply_cmd.extend(
                [
                    "-replace=hcloud_server.webrtc_worker",
                    "-replace=hcloud_server.web_worker",
                ]
            )
        terraform_step(instance_id, apply_cmd, tf_dir, "apply", timeout=1800)

        update_status(instance_id, "cluster", 4, "running", "Waiting for cluster ready")
        from services.cluster_join import mark_cluster_wait_started

        mark_cluster_wait_started(instance_id)
        master_ip = terraform_output(instance_id, "master_node_ip")
        network_id = terraform_output(instance_id, "private_network_id")
        render_platform(spec, secrets, network_id)
        fetch_kubeconfig(instance_id, master_ip)
        wait_nodes_ready(instance_id, timeout=1200)
        harden_cluster_ssh(instance_id, master_ip)

        update_status(instance_id, "workloads", 5, "running", "Labeling nodes")
        for item in plan_labels() + plan_svclb_labels():
            run_cmd(
                [
                    "kubectl",
                    "label",
                    "node",
                    item["node"],
                    f"{item['label_key']}={item['label_value']}",
                    "--overwrite",
                ],
                env=kube_env(instance_id),
            )

        update_status(instance_id, "workloads", 6, "running", "Installing CCM, CSI, storage class")
        install_ccm_csi(instance_id, spec)

        update_status(instance_id, "workloads", 7, "running", "Installing HAProxy")
        k8s = instance_dir(instance_id) / "rendered" / "k8s"
        kubectl_apply_dir(instance_id, k8s / "haproxy")

        update_status(instance_id, "workloads", 8, "running", "Installing cert-manager")
        install_cert_manager(instance_id)
        run_cmd(["kubectl", "apply", "-f", str(k8s / "cert-manager" / "cluster-issuer.yaml")], env=kube_env(instance_id))

        update_status(instance_id, "workloads", 9, "running", "Deploying HCCE")
        static_pvs = render_static_pvs(spec, secrets)
        if static_pvs:
            append_log(
                instance_id,
                f"Reattaching {static_pvs.name} — binding saved Hetzner volumes before HCCE PVCs",
            )
            run_cmd(["kubectl", "apply", "-f", str(static_pvs)], env=kube_env(instance_id), timeout=120)
        hcce = instance_dir(instance_id) / "rendered" / "hcce.yaml"
        run_cmd(["kubectl", "apply", "-f", str(hcce)], env=kube_env(instance_id), timeout=600)

        from services.volume_inventory import sync_from_cluster, wait_for_pvc_binding

        wait_for_pvc_binding(instance_id)
        inv = sync_from_cluster(instance_id, spec)
        bound = [f"{e.pvc_name}→{e.hetzner_volume_id}" for e in inv.volumes if e.hetzner_volume_id]
        if bound:
            append_log(instance_id, f"Volume inventory saved: {', '.join(bound)}")

        from services.restore_bundle_paths import local_restore_dump_path

        restore_dump = local_restore_dump_path(instance_id) if (
            spec.restore_from_bundle.enabled and spec.volume_reattach.mode == "provision_new"
        ) else None
        if restore_dump:
            update_status(instance_id, "workloads", 9, "running", "Restoring database from local restore bundle")
            append_log(instance_id, f"Applying local restore bundle dump {restore_dump.name}")
            from services.db_restore import DbRestoreError, restore_database_from_file

            try:
                restore_database_from_file(instance_id, restore_dump)
            except DbRestoreError as e:
                raise PipelineError(str(e)) from e

        lb_ip = wait_lb_public_ip(instance_id, spec, network_id, timeout=600)
        update_status(instance_id, "done", 10, "running", "Writing deployment info")
        info = build_deployment_info(spec, lb_ip=lb_ip, master_ip=master_ip, network_id=network_id)
        save_deployment_info(instance_id, info)
        update_status(instance_id, "done", 10, "succeeded", "Provisioning complete")
    except Exception as e:
        err_msg = str(e).strip() or "Provisioning failed"
        mark_pipeline_failed(instance_id, err_msg)
        raise
