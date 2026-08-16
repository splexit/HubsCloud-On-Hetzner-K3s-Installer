# Changelog

## [1.2.0] — 2026-08-15

Cluster recreate workflow, saved-instance picker on the landing page, honest infrastructure status, local config cleanup, and a simpler architecture diagram.

### Added

- **Recreate cluster flow** — after destroy, progress page shows a **Recreate cluster** banner; wizard
  review uses **Recreate** copy and `?recreate=1` opens review with reattach pre-checked. Destroy
  auto-enables `volume_reattach.mode=reattach_saved` when saved volumes and secrets exist.
- **Reattach secrets guard** — reprovision with saved volumes fails fast if `secrets.json` is missing;
  wizard and API surface `secrets_available` on volume context.
- **Saved configurations on landing** — home page lists `./data` instances by hub domain (persists across
  container restarts). Primary action **Recreate** or **Open** goes to the instance progress page — no
  separate manage route.
- **`GET /api/instances` summary** — each entry includes live Hetzner infrastructure check,
  `display_status`, saved volume count, and `can_recreate`.
- **Delete saved configuration** — `POST /api/instances/{id}/delete-config` removes local spec/secrets/logs
  under `./data/instances/<id>/` (not Hetzner volumes). Blocked while servers still exist.
- **Architecture diagram preview** — landing page shows a compact traffic-flow + three-node preview;
  **Show details** expands the full workload/PVC diagram. Visible on mobile (stacked layout).
- **Database backup downloads** — on the running cluster progress page, list and download PostgreSQL
  CronJob dumps (`db_backup-*.gz`): latest, last 7 days, or by date. Files are copied from the
  `pgsql-backups-pvc` volume via a short-lived reader pod (`GET /api/instances/{id}/db-backups`).
- **Local restore bundle** — before destroy, optionally save `restore-bundle/` (secrets, spec, SSH keys,
  volume inventory, PostgreSQL dump) so recreate does not require manually matching secrets to an old
  database. Destroy modal picks latest / date / 7-day backup; progress page can save a bundle without
  destroy. Recreate with new volumes applies the dump after HCCE deploy (`restore_from_bundle` on spec).
  Reticulum asset volume export is planned separately.
- **`GET/POST /api/instances/{id}/restore-bundle`** and **`GET .../restore-bundle.zip`** for bundle
  status, creation, and offline download.

### Changed

- Landing **Create new instance** is secondary; saved hubs are the primary entry after a restart.
- `/manage` redirects to `/` (removed “Coming soon” stub).
- Instance list status reflects **live Hetzner servers**, not stale `status.json` — destroyed clusters
  show **Stopped · volumes saved** instead of **Running**.
- [VOLUME-RECOVERY.md](VOLUME-RECOVERY.md) updated for progress-page recreate path.

### Fixed

- Stale **Running** label on the landing list when nodes were destroyed but `status.json` still said
  `succeeded`.
- **TLS ACME for hub and stream hosts** — removed `haproxy.org/ssl-redirect` from the `ret` and
  `dialog` ingresses. Catch-all `/` routes on those hosts were redirecting HTTP-01 challenges to
  HTTPS and the Reticulum/Dialog backends (403). Dialog/coturn **hostNetwork** placement on the
  WebRTC node is unchanged; HAProxy still terminates TLS and proxies to the dialog Service on 4443
  via `server-ssl`. The `nearspark` ingress keeps ssl-redirect (cors host has no `/` rule, so ACME
  is unaffected).
- **Database backup listing on BusyBox** — backup reader pod uses `wc -c` instead of GNU `stat -c`
  so listing/downloads work; empty volume shows a clear message until the first CronJob run.
- **Create backup now** — `POST /api/instances/{id}/db-backups/run` triggers the `pgsql-backup`
  CronJob immediately from the progress page (no kubectl needed).
- **Reticulum assets backup** — `POST/GET /api/instances/{id}/assets-backups` streams a
  `tar.gz` of `/storage` from the running Reticulum pod (no sidecar; `ret-pvc` stays RWO).
  Archives are saved under `./data/instances/<id>/assets-backups/` for download.
- **DB backup volume attach** — always tear down `hcce-backup-reader` after list/download
  and wait for release before starting `pgsql-backup` jobs (fixes Multi-Attach on RWO PVC).
- **`pgsql-backup` scheduling** — CronJob and backup reader pod pin to `workload-type: database`
  (master) so `pgsql-backups-pvc` does not attach to the WebRTC worker.

---

## [1.1.0] — 2026-08-06

Release of provisioning fixes, volume recovery, UI/branding refresh, and licensing/disclosure documentation.

### Fixed

- Ingress Load Balancer was created with **zero targets**, requiring a manual "attach server"
  step in the Hetzner console on every cluster. k3s ran with `--disable-cloud-controller` but
  without `--kubelet-arg=cloud-provider=external`, which the Hetzner CCM requires — without it
  the CCM never sets `spec.providerID`, so it cannot map Kubernetes nodes to Hetzner servers.
  Verified fixed by a full provisioning run: targets now attach automatically.
- Firewall hardening **destroyed and recreated all three servers**. It re-rendered the whole
  Terraform spec from `templates/` and ran `terraform apply -auto-approve` against it, so any
  template drift since provisioning (`user_data` is ForceNew) came along for the ride.
  Hardening now rewrites only the two firewall variables in the deployed `terraform.tfvars`,
  plans with `-target=hcloud_firewall.open_firewall`, verifies the plan via
  `terraform show -json`, and applies that exact saved plan. Anything other than an in-place
  firewall update aborts.
- Every `terraform apply` on every cluster wanted to replace all three servers. The hcloud
  provider never writes `location` back into state (empty on all instances) while the config
  always sets it, and `location` is ForceNew. Added `lifecycle { ignore_changes = [location] }`
  to the server resources.
- Workers failing to join the cluster. Two regressions in the cloud-init bootstrap: a
  `curl -f` readiness probe against `/readyz` (returns 401 anonymously, so the wait loop
  never exited) and an `ip route add ... scope link` for the private network (overrode
  Hetzner's gateway route and blackholed peer traffic). See
  [CLUSTER-BOOTSTRAP-POSTMORTEM.md](CLUSTER-BOOTSTRAP-POSTMORTEM.md).
- Pipeline no longer aborts on transient `ssh_unreachable` while a VM is still booting, or on
  `cloud-init: status: error` when `runcmd` is still making progress.

### Added

- **Volume inventory and reattach** — pipeline saves `volumes-inventory.json` (PVC → Hetzner volume ID)
  after HCCE deploy and on destroy; wizard step *Machines & SSH* offers reattach when saved volumes
  exist; reprovision applies static PV manifests before HCCE PVCs. See [VOLUME-RECOVERY.md](VOLUME-RECOVERY.md).
- `wait_nodes_initialized()` gate after CCM install — blocks until every node has a
  `hcloud://` providerID, and fails with an actionable message instead of letting later steps
  hang on unschedulable pods.
- Private NIC auto-detection so `--flannel-iface` survives interface renames across images.
- `kernel.apparmor_restrict_unprivileged_userns=0` for Ubuntu 24.04 containerd compatibility.
- Bootstrap stage markers surfaced in cluster diagnostics.
- 3-attempt k3s join retry so a lost etcd race self-heals.
- **Hetzner Installer branding** — Hubs-on-Hetzner hero mark, top-bar logo, "Hetzner Infrastructure" tagline.
- Navigation link to the [GitHub repository](https://github.com/splexit/HubsCloud-On-Hetzner-K3s-Installer).
- [LICENSE](../LICENSE) (MPL 2.0, same text as Hubs Foundation hubs).
- [AI-DISCLOSURE.md](../AI-DISCLOSURE.md) — honest AI usage and provenance disclosure.
- [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) — Terraform, kubectl, Helm, Hetzner provider, and runtime component licenses.

### Changed

- **k3s pinned to `v1.36.2+k3s1`** across all cloud-init templates and `cluster_repair.py`
  (`K3S_VERSION` constant) for reproducible provisioning and repair without version skew.
- hcloud Terraform provider **1.50.0 → 1.67.0**, pinned exactly in `main.tf`.
- Landing page redesigned — hero focuses on branding and actions; duplicate nav links removed from the card.
- Product name and UI copy updated to **Hetzner Installer** (page title, top bar, hero).
- Version tag in UI: **v1.1**.
- Footer disclaimer: not affiliated with hosting or the hoster; MPL 2.0 link.
- Hetzner logo size reduced in the hero brand mark.

### Known issues

- The pinned `ccm-networks.yaml` v1.23.0 carries `--cluster-cidr=10.244.0.0/16` while the
  cluster uses `10.42.0.0/16`. Cosmetic in the current setup; should be set to `10.42.0.0/16`
  when convenient.
- An instance is frozen at the spec it was rendered with, so template fixes only reach new
  instances. No migration path short of destroy + re-provision.
- hcloud provider upgrade does **not** fix `location` state drift — `ignore_changes = [location]`
  remains load-bearing. Provider bump not yet fully exercised against every live cluster variant.
- Deferred hardening work is tracked in [NEXT-STEPS.md](NEXT-STEPS.md) — etcd snapshots,
  restricting kube-API/SSH firewall source ranges, and an OS patching policy.

---

## [1.0.0] — 2026-07-24

Initial public release.

### App

- FastAPI installer with Create New Instance wizard (web UI).
- Pipeline: Terraform → k3s → CCM/CSI → HAProxy → cert-manager → curated HCCE.
- Live cluster resource polling and secrets ZIP download.
- Manage Existing stub page.

### Templates

- Redacted Terraform multi-node layout with size/location variables.
- HAProxy, cert-manager issuer, CCM/CSI secrets, metrics-server, curated `hcce.yaml.j2` with pgsql-backup.

### Redacted

- All production credentials removed from templates; placeholders only.
