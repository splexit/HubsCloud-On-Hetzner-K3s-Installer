import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, downloadUrl } from "../api";

const STEPS = [
  "Validate",
  "Render",
  "Terraform",
  "Cluster ready",
  "Labels",
  "CCM/CSI",
  "HAProxy",
  "cert-manager",
  "HCCE",
  "Done",
];

const HEALTH_GROUPS = [
  { id: "infrastructure", label: "Infrastructure" },
  { id: "platform", label: "Platform" },
  { id: "hcce", label: "HCCE" },
  { id: "tls", label: "TLS" },
  { id: "public", label: "Public" },
];

type Status = {
  phase: string;
  step: number;
  state: string;
  message: string;
  error?: string;
  intervention?: string | null;
};

type HealthCheck = {
  id: string;
  name: string;
  group: string;
  status: string;
  detail: string;
  hint?: string;
};

type HealthSummary = {
  overall: string;
  checks: HealthCheck[];
};

type Resources = {
  nodes: { name: string; status: string; labels: Record<string, string> }[];
  pods: { namespace: string; name: string; status: string; node: string; reason?: string }[];
  deployments: { namespace: string; name: string; ready: string }[];
  ingresses: { namespace: string; name: string; hosts: string[] }[];
  certificates: { namespace: string; name: string; ready: boolean }[];
  loadbalancers: { name: string; external_ip: string; note?: string }[];
  error?: string;
};

type DeploymentInfo = {
  hub_domain?: string;
  admin_url?: string;
  lb_ip?: string;
  dns_pending?: boolean;
  dns_records?: { host: string; type: string; target: string }[];
};

type ClusterJoinServer = {
  name: string;
  role: string;
  public_ip: string;
  private_ip: string;
  hetzner_status: string;
  k8s_ready: boolean;
  k8s_present: boolean;
  issue: string | null;
  bootstrap_stage?: string | null;
  bootstrap_log?: string | null;
};

type ClusterJoinStatus = {
  expected_nodes: number;
  joined_ready: number;
  joined_not_ready: number;
  missing: string[];
  stuck_seconds: number;
  servers: ClusterJoinServer[];
  suggested_action: string;
  error?: string | null;
};

type HetznerAudit = {
  clean: boolean;
  issues: string[];
  warnings?: string[];
  has_billable_leftovers?: boolean;
  servers: { name?: string; id?: number }[];
  networks: { name?: string; id?: number }[];
  firewalls: { name?: string; id?: number }[];
  load_balancers: { name?: string; id?: number }[];
  volumes?: number[];
};

type VolumeInventoryEntry = {
  pvc_name: string;
  role: string;
  size: string;
  hetzner_volume_id: number | null;
  hetzner_volume_name: string | null;
  status: string;
  bound_at: string | null;
};

type VolumesContext = {
  reattach_eligible: boolean;
  secrets_available: boolean;
  inventory: { volumes: VolumeInventoryEntry[]; updated_at?: string };
  entries: {
    pvc_name: string;
    hetzner_volume_id: number | null;
    status: string;
    reattach_eligible: boolean;
  }[];
};

const PVC_LABELS: Record<string, string> = {
  "pgsql-pvc": "PostgreSQL database",
  "ret-pvc": "Reticulum",
  "pgsql-backups-pvc": "PostgreSQL backup",
};

function issueLabel(issue: string | null) {
  switch (issue) {
    case "private_network_down":
      return "Private network problem";
    case "cloud_init_running":
      return "Setting up";
    case "cloud_init_error":
      return "Cloud-init failed";
    case "k3s_service_failed":
      return "K3s service failed";
    case "k3s_inactive":
      return "Kubernetes not running";
    case "ssh_unreachable":
      return "Cannot connect via SSH";
    case "ssh_key_mismatch":
      return "SSH key rejected (stale server?)";
    case "hetzner_missing":
      return "Server missing in Hetzner";
    default:
      return issue ? issue.replace(/_/g, " ") : null;
  }
}

function bootstrapStageLabel(stage: string | null | undefined): string | null {
  if (!stage) return null;
  if (stage.startsWith("failed:")) {
    return `Failed: ${stage.slice(7).replace(/-/g, " ")}`;
  }
  if (stage.startsWith("cloud-init:")) {
    return stage.replace("cloud-init:", "Cloud-init: ");
  }
  const match = stage.match(/^k3s-join-(\d+)$/);
  if (match) return `Joining cluster (attempt ${match[1]}/5)`;
  const labels: Record<string, string> = {
    starting: "Starting boot…",
    netplan: "Configuring network…",
    "wait-private-network": "Waiting for private network…",
    "wait-master-grace": "Waiting for master to start…",
    "wait-master-api": "Waiting for master API…",
    "wait-master-readyz": "Waiting for master to be ready…",
    "k3s-install": "Installing K3s…",
    "wait-k3s-config": "Waiting for K3s config…",
    "wait-readyz": "Waiting for API readiness…",
    "wait-k3s-active": "Waiting for K3s service…",
    "setup-user": "Finishing setup…",
    done: "Bootstrap complete",
  };
  return labels[stage] || stage.replace(/-/g, " ");
}

function serverStatusLabel(server: ClusterJoinServer, stuckMinutes: number) {
  if (server.k8s_ready) return "Connected";
  if (server.k8s_present) return "Joining";
  const bootstrap = bootstrapStageLabel(server.bootstrap_stage);
  if (bootstrap) return bootstrap;
  // Workers often need 10+ minutes for cloud-init + k3s join — avoid alarming labels early on.
  if (stuckMinutes < 10 && server.issue && server.issue !== "ssh_unreachable" && server.issue !== "ssh_key_mismatch" && server.issue !== "hetzner_missing") {
    return "Setting up";
  }
  const issue = issueLabel(server.issue);
  if (issue) return issue;
  return "Setting up";
}

function interventionHint(intervention?: string | null) {
  switch (intervention) {
    case "server_types":
      return "The selected Hetzner server types are invalid or unavailable in this region. Open Machines & SSH, pick types from the live catalog, validate, then retry.";
    case "credentials":
      return "Hetzner rejected the API token. Update credentials in the wizard, then retry.";
    case "firewall":
      return "Firewall rules could not be updated. Your cluster is still running. Click Apply hardened firewall again, or leave the open firewall in place (less secure).";
    case "destroy":
      return "Could not delete all Hetzner resources automatically. Check the activity log, finish any remaining deletes in the Hetzner Console, then retry Destroy or provision again.";
    case "cluster_join":
      return "The pipeline diagnosed the problem automatically — read the error above for the root cause and per-node status. Try automatic repair first (safe — does not delete servers). If bootstrap failed on a worker, use Retry provisioning to recreate workers with fresh cloud-init.";
    default:
      return "Review the error and activity log, fix the configuration, then retry provisioning.";
  }
}

function overallLabel(overall: string) {
  switch (overall) {
    case "ok":
      return "Healthy";
    case "warn":
      return "Degraded";
    case "fail":
      return "Unhealthy";
    default:
      return "Checking…";
  }
}

function overallColor(overall: string) {
  switch (overall) {
    case "ok":
      return "var(--ok)";
    case "warn":
      return "var(--signal)";
    case "fail":
      return "var(--bad)";
    default:
      return "var(--line)";
  }
}

function statusIcon(status: string) {
  switch (status) {
    case "ok":
      return "✓";
    case "warn":
      return "!";
    case "fail":
      return "✗";
    case "skip":
      return "–";
    default:
      return "…";
  }
}

export function ProgressPage({ instanceId }: { instanceId: string }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [resources, setResources] = useState<Resources | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [info, setInfo] = useState<DeploymentInfo | null>(null);
  const [instanceSpec, setInstanceSpec] = useState<{ firewall_hardened?: boolean; firewall_allow_ssh?: boolean } | null>(null);
  const [firewallAllowSsh, setFirewallAllowSsh] = useState(true);
  const [hardeningFirewall, setHardeningFirewall] = useState(false);
  const [started, setStarted] = useState<number>(Date.now());
  const [retrying, setRetrying] = useState(false);
  const [joinStatus, setJoinStatus] = useState<ClusterJoinStatus | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [aborting, setAborting] = useState(false);
  const [destroying, setDestroying] = useState(false);
  const [hetznerAudit, setHetznerAudit] = useState<HetznerAudit | null>(null);
  const [volumesCtx, setVolumesCtx] = useState<VolumesContext | null>(null);
  const [syncingVolumes, setSyncingVolumes] = useState(false);

  const installerProgressUrl = useMemo(
    () => `${window.location.origin}/instances/${instanceId}`,
    [instanceId]
  );

  const secretsDownloadUrl = useMemo(() => {
    const params = new URLSearchParams({ progress_url: installerProgressUrl });
    return downloadUrl(`/instances/${instanceId}/secrets-bundle.zip?${params.toString()}`);
  }, [instanceId, installerProgressUrl]);

  const debugDownloadUrl = useMemo(
    () => downloadUrl(`/instances/${instanceId}/debug-bundle.zip`),
    [instanceId]
  );

  useEffect(() => {
    const load = async () => {
      try {
        const d = await api<{
          status: Status;
          deployment_info?: DeploymentInfo;
          spec?: { firewall_hardened?: boolean; firewall_allow_ssh?: boolean };
        }>(`/instances/${instanceId}`);
        setStatus(d.status);
        if (d.deployment_info) setInfo(d.deployment_info);
        if (d.spec) {
          setInstanceSpec(d.spec);
          if (typeof d.spec.firewall_allow_ssh === "boolean") {
            setFirewallAllowSsh(d.spec.firewall_allow_ssh);
          }
        }
        const l = await api<{ lines: string[] }>(`/instances/${instanceId}/logs?lines=200`);
        setLogs(l.lines);
      } catch {
        /* keep last known status on transient poll errors */
      }
    };
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [instanceId]);

  useEffect(() => {
    const loadVolumes = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const v = await api<VolumesContext>(`/instances/${instanceId}/volumes`);
        setVolumesCtx(v);
      } catch {
        /* ignore */
      }
    };
    loadVolumes();
    const t = setInterval(loadVolumes, 15000);
    return () => clearInterval(t);
  }, [instanceId]);

  const syncVolumes = async () => {
    setSyncingVolumes(true);
    try {
      const v = await api<VolumesContext>(`/instances/${instanceId}/sync-volumes`, { method: "POST" });
      setVolumesCtx(v);
    } finally {
      setSyncingVolumes(false);
    }
  };

  useEffect(() => {
    const poll = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const r = await api<Resources>(`/instances/${instanceId}/resources`);
        setResources(r);
        const nodes = r?.nodes || [];
        const ready = nodes.filter((n) => n.status === "True").length;
        if (nodes.length >= 3 && ready >= 3) {
          const h = await api<HealthSummary>(`/instances/${instanceId}/health`);
          setHealth(h);
        }
      } catch {
        /* ignore transient poll errors */
      }
    };
    poll();
    const t = setInterval(poll, 4000);
    return () => clearInterval(t);
  }, [instanceId]);

  useEffect(() => {
    const pollJoin = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const js = await api<ClusterJoinStatus>(`/instances/${instanceId}/cluster-join-status`);
        setJoinStatus(js);
      } catch {
        /* ignore */
      }
    };
    pollJoin();
    const t = setInterval(pollJoin, 5000);
    return () => clearInterval(t);
  }, [instanceId]);

  useEffect(() => {
    const pollAudit = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const audit = await api<HetznerAudit>(`/instances/${instanceId}/hetzner/audit`);
        setHetznerAudit(audit);
      } catch {
        /* token may be missing during wizard */
      }
    };
    pollAudit();
    const t = setInterval(pollAudit, 15000);
    return () => clearInterval(t);
  }, [instanceId]);

  const retry = async () => {
    setRetrying(true);
    try {
      await api(`/instances/${instanceId}/retry`, { method: "POST" });
      setStarted(Date.now());
    } finally {
      setRetrying(false);
    }
  };

  const applyFirewallHardening = async () => {
    setHardeningFirewall(true);
    try {
      await api(`/instances/${instanceId}/harden-firewall`, {
        method: "POST",
        body: JSON.stringify({ allow_ssh: firewallAllowSsh }),
      });
    } finally {
      setHardeningFirewall(false);
    }
  };

  const repairClusterJoin = async () => {
    setRepairing(true);
    try {
      await api(`/instances/${instanceId}/repair-cluster-join`, { method: "POST" });
    } finally {
      setRepairing(false);
    }
  };

  const abortProvisioning = async () => {
    setAborting(true);
    try {
      await api(`/instances/${instanceId}/abort`, { method: "POST" });
    } finally {
      setAborting(false);
    }
  };

  const destroyAll = async () => {
    const ok = window.confirm(
      "Destroy cluster infrastructure in Hetzner?\n\n" +
        "This runs terraform destroy and removes servers, private network, firewall, " +
        "placement group, and SSH key resources.\n\n" +
        "Load balancer(s) and block volumes created by Kubernetes (ingress + PostgreSQL data) " +
        "are NOT deleted — they keep billing until you remove them in Hetzner Console. " +
        "That is intentional so database data is not destroyed accidentally.\n\n" +
        "Wizard settings and secrets are kept so you can provision again."
    );
    if (!ok) return;
    setDestroying(true);
    try {
      await api(`/instances/${instanceId}/destroy`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
    } finally {
      setDestroying(false);
    }
  };

  const elapsed = Math.floor((Date.now() - started) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;

  const isRunning = status?.state === "running";
  const isFirewallPhase = status?.phase === "firewall";
  const isFirewallRunning = isFirewallPhase && isRunning;
  const isDestroyRunning = status?.phase === "destroy" && isRunning;
  const clusterJoinRunning = isRunning && status?.phase === "cluster";
  const clusterJoinFailed = status?.state === "failed" && status?.intervention === "cluster_join";
  const showClusterJoinPanel = clusterJoinRunning || clusterJoinFailed;
  const showDestroy =
    !isDestroyRunning &&
    ((status?.step || 0) > 0 ||
      status?.state === "succeeded" ||
      status?.state === "failed" ||
      status?.phase === "destroy" ||
      (isRunning && status?.phase !== "destroy"));
  const joinedCount = joinStatus?.joined_ready ?? 0;
  const expectedNodes = joinStatus?.expected_nodes ?? 3;
  const stuckMinutes = joinStatus ? Math.floor(joinStatus.stuck_seconds / 60) : 0;
  const phaseLabel =
    isDestroyRunning
      ? "Destroying resources"
      : isFirewallPhase && isRunning
      ? "Hardening firewall"
      : status?.state === "succeeded"
      ? "Ready"
      : status?.state === "failed"
        ? "Failed"
        : isRunning
          ? (status.step <= 4 ? "Building cluster" : "Installing workloads")
          : volumesCtx?.reattach_eligible && volumesCtx.secrets_available && hetznerAudit?.clean
            ? "Ready to recreate"
            : "Not started";

  const recreateReady =
    !isRunning &&
    status?.state === "pending" &&
    status?.phase === "idle" &&
    Boolean(volumesCtx?.reattach_eligible && volumesCtx.secrets_available) &&
    Boolean(hetznerAudit?.clean);

  const recreateLink = `/instances/${instanceId}/setup?recreate=1`;

  const fixLink =
    status?.intervention === "server_types"
      ? `/instances/${instanceId}/setup?step=2`
      : status?.intervention === "credentials"
        ? `/instances/${instanceId}/setup?step=0`
        : status?.intervention === "cluster_join"
          ? `/instances/${instanceId}`
          : `/instances/${instanceId}/setup?step=5`;

  const hcceDeployments = (resources?.deployments || []).filter((d) => d.namespace === "hcce");
  const hcceCerts = (resources?.certificates || []).filter((c) => c.namespace === "hcce");
  const problemPods = (resources?.pods || []).filter(
    (p) =>
      p.namespace === "hcce" &&
      (p.status === "Failed" ||
        p.status === "Pending" ||
        p.reason === "CrashLoopBackOff" ||
        p.reason === "ImagePullBackOff" ||
        p.reason === "CreateContainerConfigError")
  );

  const clusterNodes = resources?.nodes || [];
  const readyNodeCount = clusterNodes.filter((n) => n.status === "True").length;
  const clusterNodesReady = clusterNodes.length >= 3 && readyNodeCount >= 3;
  const showHealth = clusterNodesReady || status?.state === "succeeded";
  const workloadsVisible = clusterNodesReady && hcceDeployments.length > 0;
  const showFirewallStep =
    workloadsVisible &&
    (status?.state === "succeeded" || isFirewallPhase) &&
    !instanceSpec?.firewall_hardened;
  const firewallHardened = Boolean(instanceSpec?.firewall_hardened);

  const statusDotClass =
    status?.state === "failed"
      ? "status-dot-fail"
      : status?.state === "succeeded"
        ? "status-dot-ok"
        : isRunning
          ? "status-dot-running"
          : "status-dot-idle";

  const hubDomain = info?.hub_domain || "";

  return (
    <div className="progress-page">
      <div className="progress-overview">
      <div className="progress-banner">
        <div className="progress-banner-left">
          <span className={`status-dot ${statusDotClass}`} aria-hidden />
          <span className="progress-phase">{phaseLabel}</span>
          {isRunning && <span className="muted" style={{ fontSize: 13 }}>Live</span>}
        </div>
        {hubDomain ? <span className="progress-domain">{hubDomain}</span> : <span />}
        <span className="progress-elapsed">
          {mins}m {String(secs).padStart(2, "0")}s
        </span>
      </div>

      <div className="pipeline-timeline" role="list" aria-label="Provisioning steps">
        {STEPS.map((label, i) => {
          const n = i + 1;
          const done = (status?.step || 0) > n || status?.state === "succeeded";
          const current = status?.step === n && status?.state === "running";
          const failed = status?.state === "failed" && status?.step === n;
          const cls = failed ? "is-failed" : current ? "is-current" : done ? "is-done" : "";
          return (
            <div key={label} className={`timeline-step ${cls}`} role="listitem">
              {label}
            </div>
          );
        })}
      </div>
      </div>

      {recreateReady && (
        <section className="panel recreate-banner">
          <h2 style={{ marginBottom: 8 }}>Recreate cluster</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Infrastructure is torn down but saved secrets and Hetzner block volumes are available. Recreate rebuilds
            servers and Kubernetes, rebinds your database and upload volumes, then redeploys HCCE.
          </p>
          <ul className="topo-list" style={{ margin: "12px 0", paddingLeft: 0, listStyle: "none" }}>
            {(volumesCtx?.entries || [])
              .filter((e) => e.reattach_eligible)
              .map((entry) => (
                <li key={entry.pvc_name} className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
                  {PVC_LABELS[entry.pvc_name] || entry.pvc_name} → Hetzner #{entry.hetzner_volume_id}
                </li>
              ))}
          </ul>
          <Link to={recreateLink} className="btn btn-primary" style={{ textDecoration: "none", marginTop: 8 }}>
            Recreate cluster
          </Link>
        </section>
      )}

      {status?.state === "failed" && (
        <div className="panel failure-banner">
          <strong className="bad-text failure-error">{status.error || status.message}</strong>
          <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
            {interventionHint(status.intervention)}
          </p>
          <div style={{ display: "flex", gap: 12, marginTop: 16, flexWrap: "wrap" }}>
            {status.intervention !== "cluster_join" && status.intervention !== "firewall" && (
              <Link to={fixLink} className="btn btn-outline" style={{ textDecoration: "none" }}>
                {status.intervention === "server_types" ? "Fix server types" : "Open wizard"}
              </Link>
            )}
            {(status.intervention === "cluster_join" || clusterJoinFailed) && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={repairing}
                onClick={() => void repairClusterJoin()}
              >
                {repairing ? "Repairing…" : "Try automatic repair"}
              </button>
            )}
            <button type="button" className="btn btn-primary" disabled={retrying} onClick={() => void retry()}>
              {retrying ? "Retrying…" : "Retry provisioning"}
            </button>
            <a href={debugDownloadUrl} className="btn btn-outline" style={{ textDecoration: "none" }}>
              Download debug bundle
            </a>
          </div>
        </div>
      )}

      {showClusterJoinPanel && joinStatus && (
        <section className="panel">
          <h2 style={{ marginBottom: 8 }}>Cluster join progress</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Your servers exist in Hetzner. We're waiting for all three to join the cluster. This usually takes 10–15
            minutes — the WebRTC worker often finishes last.
          </p>
          <p style={{ marginTop: 12, fontWeight: 600 }}>
            {joinedCount} of {expectedNodes} nodes connected
          </p>
          {stuckMinutes >= 10 && clusterJoinRunning && (
            <p className={stuckMinutes >= 20 ? "bad-text" : "muted"} style={{ marginTop: 8 }}>
              {stuckMinutes >= 20
                ? `This is taking longer than usual (${stuckMinutes} min). Missing: ${joinStatus.missing.join(", ") || "—"}. You can try automatic repair below.`
                : "Still setting up — this can take a while. If nothing changes after 20 minutes, try automatic repair below (your servers will not be deleted)."}
            </p>
          )}
          <div style={{ display: "grid", gap: 10, marginTop: 16 }}>
            {joinStatus.servers.map((s) => (
              <div
                key={s.name}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "10px 12px",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius-sm)",
                  background: "var(--bg-subtle)",
                }}
              >
                <div>
                  <strong>{s.name}</strong>
                  <span className="muted" style={{ marginLeft: 8, fontSize: 13 }}>
                    {s.role}
                  </span>
                  {!s.k8s_ready && s.bootstrap_stage && (
                    <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                      {s.bootstrap_stage}
                      {s.bootstrap_log ? ` — ${s.bootstrap_log.replace(/^\d{4}-\d{2}-\d{2}T[^ ]+ /, "").slice(0, 80)}` : ""}
                    </div>
                  )}
                </div>
                <span style={{ fontSize: 13 }}>{serverStatusLabel(s, stuckMinutes)}</span>
              </div>
            ))}
          </div>
          {clusterJoinRunning && (
            <div style={{ display: "flex", gap: 12, marginTop: 16, flexWrap: "wrap" }}>
              <button
                type="button"
                className="btn btn-primary"
                disabled={repairing}
                onClick={() => void repairClusterJoin()}
              >
                {repairing ? "Repair running…" : "Try automatic repair"}
              </button>
              <button
                type="button"
                className="btn btn-outline"
                disabled={aborting}
                onClick={() => void abortProvisioning()}
              >
                {aborting ? "Stopping…" : "Stop waiting and mark as failed"}
              </button>
              <a href={debugDownloadUrl} className="btn btn-outline" style={{ textDecoration: "none" }}>
                Download debug bundle
              </a>
            </div>
          )}
        </section>
      )}

      {status?.state === "succeeded" && (
        <section className="panel">
          <h2 style={{ marginBottom: 8 }}>Handoff</h2>
          <p className="muted" style={{ marginTop: 0 }}>Workloads install automatically after the cluster is ready.</p>
          {firewallHardened && (
            <p className="ok-text" style={{ marginTop: 0 }}>
              Hardened Hetzner firewall applied
              {instanceSpec?.firewall_allow_ssh ? " (SSH port 22 open)." : " (SSH port 22 closed)."}
            </p>
          )}
          {info?.dns_records && info.dns_records.length > 0 ? (
            <div style={{ marginTop: 16 }}>
              <h3>DNS checklist</h3>
              <p className="muted">Point these A records at the Hetzner load balancer public IP (not a node private IP).</p>
              <ul>
                {info.dns_records.map((r) => (
                  <li key={r.host}>
                    <code>{r.host}</code> → {r.target}
                  </li>
                ))}
              </ul>
            </div>
          ) : info?.dns_pending ? (
            <p className="muted" style={{ marginTop: 16 }}>
              DNS checklist pending — waiting for the Hetzner load balancer public IP. Refresh this page in a minute.
            </p>
          ) : null}
          {info?.lb_ip && (
            <p style={{ marginTop: 12 }}>
              Load balancer: <code>{info.lb_ip}</code>
            </p>
          )}
          {info?.admin_url && (
            <p>
              Admin: <a href={info.admin_url}>{info.admin_url}</a>
            </p>
          )}
          <a href={secretsDownloadUrl} className="btn btn-primary" style={{ marginTop: 16, display: "inline-block", textDecoration: "none" }}>
            Download secrets ZIP
          </a>
          <p className="muted" style={{ marginTop: 8 }}>
            Contains plaintext passwords, kubeconfig, SSH keys under <code>ssh/</code>, the rendered{" "}
            <code>hcce.yaml</code> applied to the cluster, platform manifests under <code>rendered/k8s/</code>,{" "}
            <code>volumes-inventory.json</code> (PVC → Hetzner volume IDs), and{" "}
            <code>README.txt</code> with this installer URL (<code>{installerProgressUrl}</code>) so you can reopen this
            instance after rebuilding the container. Store offline; localhost-only API.
          </p>
        </section>
      )}

      {showFirewallStep && (
        <section className="panel">
          <h2 style={{ marginBottom: 8 }}>Firewall hardening</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Nodes currently use an open firewall for provisioning. Replace it with HCCE-required inbound rules via
            Terraform:
          </p>
          <ul className="muted" style={{ marginTop: 8, paddingLeft: 18 }}>
            <li>TCP 80, 443, 6443, 4443, 5349, 31621, 32471</li>
            <li>UDP 35000–60000 (WebRTC media)</li>
          </ul>
          <label className="field" style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 16, marginBottom: 0 }}>
            <input
              type="checkbox"
              checked={firewallAllowSsh}
              disabled={hardeningFirewall || isFirewallRunning}
              onChange={(e) => setFirewallAllowSsh(e.target.checked)}
            />
            <span>Keep SSH (port 22) open for direct node access</span>
          </label>
          <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
            Uncheck to close port 22 on public node IPs after hardening. You can still use the SSH key from the secrets
            ZIP if you leave it open.
          </p>
          {status?.phase === "firewall" && status.state === "failed" && (
            <p className="bad-text" style={{ marginTop: 12 }}>
              {status.error || status.message}
            </p>
          )}
          {status?.phase === "firewall" && status.state === "failed" && (
            <p className="muted" style={{ marginTop: 8 }}>
              {interventionHint("firewall")}
            </p>
          )}
          <button
            type="button"
            className="btn btn-primary"
            style={{ marginTop: 16 }}
            disabled={hardeningFirewall || isFirewallRunning}
            onClick={() => void applyFirewallHardening()}
          >
            {hardeningFirewall || isFirewallRunning ? "Applying hardened firewall…" : "Apply hardened firewall"}
          </button>
        </section>
      )}

      <div className="split-pane-cluster">
      <div className="split-pane">
        <div className="split-pane-card">
          <h3 className="section-title">
            Activity log
            {isRunning && (
              <span className="muted" style={{ fontWeight: 400, marginLeft: 8, textTransform: "none", letterSpacing: 0 }}>
                · Live
              </span>
            )}
          </h3>
          <div className="mono-panel" style={{ maxHeight: 360, border: "none", padding: 12, background: "var(--bg-subtle)" }}>
            {logs.join("\n") || "Waiting for logs…"}
          </div>
        </div>
        <div className="split-pane-card">
          <h3 className="section-title">Cluster snapshot</h3>
          {resources?.error && <p className="bad-text">{resources.error}</p>}
          <div style={{ overflow: "hidden", borderRadius: "var(--radius-sm)", border: "1px solid var(--border)" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th colSpan={3}>Nodes ({resources?.nodes?.length || 0})</th>
                </tr>
                <tr>
                  <th>Name</th>
                  <th>Ready</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                {(resources?.nodes || []).map((n) => (
                  <tr key={n.name}>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>{n.name}</td>
                    <td>{n.status === "True" ? "Ready" : n.status}</td>
                    <td className="muted">{n.labels["workload-type"] || "—"}</td>
                  </tr>
                ))}
                {(resources?.nodes || []).length === 0 && (
                  <tr><td colSpan={3} className="muted">None yet</td></tr>
                )}
              </tbody>
            </table>

            <table className="data-table">
              <thead>
                <tr>
                  <th colSpan={2}>HCCE deployments ({hcceDeployments.length})</th>
                </tr>
                <tr>
                  <th>Name</th>
                  <th>Ready</th>
                </tr>
              </thead>
              <tbody>
                {hcceDeployments.map((d) => (
                  <tr key={d.name}>
                    <td>{d.name}</td>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>{d.ready}</td>
                  </tr>
                ))}
                {hcceDeployments.length === 0 && (
                  <tr><td colSpan={2} className="muted">None yet</td></tr>
                )}
              </tbody>
            </table>

            <table className="data-table">
              <thead>
                <tr>
                  <th colSpan={2}>TLS certificates ({hcceCerts.length})</th>
                </tr>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {hcceCerts.map((c) => (
                  <tr key={c.name}>
                    <td>{c.name}</td>
                    <td>{c.ready ? "Ready" : "Not ready"}</td>
                  </tr>
                ))}
                {hcceCerts.length === 0 && (
                  <tr><td colSpan={2} className="muted">None yet</td></tr>
                )}
              </tbody>
            </table>

            {problemPods.length > 0 && (
              <table className="data-table">
                <thead>
                  <tr>
                    <th colSpan={2}>Problem pods</th>
                  </tr>
                </thead>
                <tbody>
                  {problemPods.slice(0, 8).map((p) => (
                    <tr key={p.name} className="row-bad">
                      <td>{p.name}</td>
                      <td className="bad-text">
                        {p.status}{p.reason ? ` (${p.reason})` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <table className="data-table">
              <thead>
                <tr>
                  <th>Load balancer (Hetzner API)</th>
                </tr>
              </thead>
              <tbody>
                {(resources?.loadbalancers || []).map((lb) => (
                  <tr key={lb.name}>
                    <td>
                      <span style={{ fontFamily: "var(--font-mono)", fontSize: 14 }}>{lb.external_ip || "resolving…"}</span>
                      {lb.note ? <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>{lb.note}</span> : null}
                    </td>
                  </tr>
                ))}
                {(resources?.loadbalancers || []).length === 0 && (
                  <tr><td className="muted">None yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      </div>

      {!clusterNodesReady && isRunning && (status?.step || 0) >= 4 && !showClusterJoinPanel && (
        <p className="muted" style={{ marginTop: 16 }}>
          Install health checks start once all 3 nodes are Ready ({readyNodeCount}/{expectedNodes}).
        </p>
      )}

      {showHealth && health && health.checks.length > 0 && (
        <div className="health-cluster">
        <section className="panel">
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
            <span
              style={{
                display: "inline-block",
                padding: "4px 12px",
                borderRadius: "var(--radius-sm)",
                background: overallColor(health.overall),
                color: health.overall === "unknown" ? "var(--text-secondary)" : "white",
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              {overallLabel(health.overall)}
            </span>
            <span className="muted">Install health — polled every 4s</span>
          </div>
          <div className="health-grid">
            {HEALTH_GROUPS.map(({ id, label }) => {
              const groupChecks = health.checks.filter((c) => c.group === id);
              if (groupChecks.length === 0) return null;
              return (
                <div key={id} className="health-group">
                  <h4 style={{ margin: "0 0 8px", fontSize: 13, color: "var(--text-secondary)" }}>{label}</h4>
                  <ul className="health-list">
                    {groupChecks.map((c) => (
                      <li key={c.id} className={`health-item health-${c.status}`}>
                        <span className="health-icon" aria-hidden>{statusIcon(c.status)}</span>
                        <div>
                          <strong>{c.name}</strong>
                          <span className="muted" style={{ marginLeft: 8 }}>{c.detail}</span>
                          {c.hint ? <p className="health-hint">{c.hint}</p> : null}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </section>
        </div>
      )}

      {(volumesCtx?.inventory?.volumes || []).some((v) => v.hetzner_volume_id) && status?.state !== "running" && (
        <section className="panel" style={{ marginTop: 24 }}>
          <h2 style={{ marginBottom: 8 }}>Volume inventory</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            PVC → Hetzner volume mapping for this instance.
            {recreateReady ? (
              <> Use <Link to={recreateLink}>Recreate cluster</Link> to rebind these volumes.</>
            ) : (
              <> Use <Link to={`/instances/${instanceId}/setup?step=2`}>Machines &amp; SSH</Link> to reattach after destroy.</>
            )}
          </p>
          <table className="data-table" style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th>PVC</th>
                <th>Hetzner ID</th>
                <th>Size</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {volumesCtx!.inventory.volumes.map((v) => (
                <tr key={v.pvc_name}>
                  <td>{PVC_LABELS[v.pvc_name] || v.pvc_name}</td>
                  <td style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>{v.hetzner_volume_id ?? "—"}</td>
                  <td>{v.size}</td>
                  <td>{v.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            type="button"
            className="btn btn-outline"
            style={{ marginTop: 12 }}
            disabled={syncingVolumes || !status || status.state === "running"}
            onClick={() => void syncVolumes()}
          >
            {syncingVolumes ? "Syncing…" : "Sync from cluster"}
          </button>
        </section>
      )}

      {showDestroy && (
        <section className="panel" style={{ marginTop: 24, borderColor: "var(--bad)" }}>
          <h2 style={{ marginBottom: 8 }}>Danger zone</h2>
          {hetznerAudit && (
            <>
              <p
                className={
                  !hetznerAudit.clean
                    ? status?.state === "succeeded"
                      ? "ok-text"
                      : "bad-text"
                    : hetznerAudit.has_billable_leftovers
                      ? "warn-text"
                      : "ok-text"
                }
                style={{ marginTop: 0, marginBottom: 8 }}
              >
                {!hetznerAudit.clean
                  ? status?.state === "succeeded"
                    ? "Cluster is running — servers, network, and firewall are present as expected."
                    : `Hetzner audit: ${hetznerAudit.issues.length} Terraform-managed resource(s) still present — destroy or remove in Console before reprovisioning.`
                  : hetznerAudit.has_billable_leftovers
                    ? "Hetzner audit: cluster infrastructure is gone. Load balancer(s) and/or block volumes from Kubernetes still bill — intentional (database data kept)."
                    : "Hetzner audit: no HCCE servers, network, or firewall found."}
              </p>
              {!hetznerAudit.clean && hetznerAudit.issues.length > 0 && (
                <ul className="muted" style={{ marginTop: 0, paddingLeft: 18, fontSize: 13 }}>
                  {hetznerAudit.issues.slice(0, 6).map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              )}
              {hetznerAudit.clean && hetznerAudit.warnings && hetznerAudit.warnings.length > 0 && (
                <ul className="muted" style={{ marginTop: 0, paddingLeft: 18, fontSize: 13 }}>
                  {hetznerAudit.warnings.slice(0, 6).map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              )}
            </>
          )}
          <p className="muted" style={{ marginTop: hetznerAudit ? 8 : 0 }}>
            Tear down Terraform-managed infrastructure: three servers, private network, firewall,
            placement group, and SSH key. Kubernetes-created ingress load balancer(s) and CSI block
            volumes (PostgreSQL / Reticulum data) are left in Hetzner on purpose and continue billing
            until you delete them in the Console. Wizard settings and secrets are kept.
          </p>
          {status?.phase === "destroy" && status.state === "failed" && (
            <p className="bad-text" style={{ marginTop: 12 }}>
              {status.error || status.message}
            </p>
          )}
          {status?.phase !== "destroy" && status?.state === "pending" && hetznerAudit?.has_billable_leftovers && (
            <p className="warn-text" style={{ marginTop: 12 }}>
              Some Hetzner resources still bill after destroy — see audit above. Delete load balancer(s)
              and block volume(s) in Hetzner Console when you no longer need the data.
            </p>
          )}
          <button
            type="button"
            className="btn btn-outline bad-text"
            style={{ marginTop: 16, borderColor: "var(--bad)" }}
            disabled={destroying || isDestroyRunning || (isRunning && status?.phase === "terraform")}
            onClick={() => void destroyAll()}
          >
            {destroying || isDestroyRunning ? "Destroying cluster infrastructure…" : "Destroy cluster infrastructure"}
          </button>
          {isRunning && status?.phase === "terraform" && (
            <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
              Wait for Terraform apply to finish before destroying.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
