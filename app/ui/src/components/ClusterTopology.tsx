import { useId, useState } from "react";

export type ClusterTopologyProps = {
  compact?: boolean;
  /** Landing-style minimal diagram with optional expand toggle */
  preview?: boolean;
  expandable?: boolean;
  defaultExpanded?: boolean;
};

const MASTER_WORKLOADS = [
  "PostgreSQL",
  "PgBouncer",
  "Reticulum",
  "HAProxy Ingress controller",
  "pgsql-backup (CronJob)",
];

const WEB_WORKLOADS = ["Hubs", "Spoke", "Nearspark", "Photomnemonic"];

const WEBRTC_WORKLOADS = ["Dialog", "Coturn"];

const MASTER_VOLUMES = ["pgsql-pvc", "pgsql-backups-pvc", "ret-pvc"];

const PREVIEW_NODES = [
  {
    id: "master",
    title: "Master",
    role: "database",
    summary: "PostgreSQL, Reticulum, HAProxy",
    volumes: "3 persistent volumes",
    accent: "database",
  },
  {
    id: "web",
    title: "Web worker",
    role: "web",
    summary: "Hubs, Spoke, static assets",
    volumes: null,
    accent: "web",
  },
  {
    id: "webrtc",
    title: "WebRTC worker",
    role: "webrtc",
    summary: "Dialog, Coturn (media)",
    volumes: null,
    accent: "webrtc",
  },
] as const;

function NodeCard({
  title,
  role,
  extra,
  workloads,
  volumes,
  volumeNote,
  footnote,
}: {
  title: string;
  role: string;
  extra?: string;
  workloads: string[];
  volumes?: string[];
  volumeNote?: string;
  footnote?: string;
}) {
  return (
    <article className="topo-node">
      <header className="topo-node-head">
        <h4 className="topo-node-title">{title}</h4>
        <p className="topo-node-role">
          <span className="topo-label">workload-type:</span> {role}
        </p>
        {extra ? <p className="topo-node-extra">{extra}</p> : null}
      </header>
      <div className="topo-node-section">
        <p className="topo-section-label">Workloads</p>
        <ul className="topo-list">
          {workloads.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      </div>
      <div className="topo-node-section">
        <p className="topo-section-label">Volumes</p>
        {volumes && volumes.length > 0 ? (
          <ul className="topo-list topo-volumes">
            {volumes.map((name) => (
              <li key={name}>
                <span className="topo-disk" aria-hidden>
                  ◐
                </span>
                {name}
              </li>
            ))}
          </ul>
        ) : (
          <p className="topo-muted">{volumeNote || "No persistent volumes"}</p>
        )}
        {footnote ? <p className="topo-footnote">{footnote}</p> : null}
      </div>
    </article>
  );
}

function TopoPreview() {
  return (
    <div className="topo-preview" aria-hidden={false}>
      <div className="topo-flow">
        <div className="topo-flow-step">
          <span className="topo-flow-badge" aria-hidden>
            IN
          </span>
          <span className="topo-flow-label">Internet</span>
        </div>
        <span className="topo-flow-arrow" aria-hidden />
        <div className="topo-flow-step topo-flow-step-accent">
          <span className="topo-flow-badge" aria-hidden>
            LB
          </span>
          <span className="topo-flow-label">Hetzner LB</span>
        </div>
        <span className="topo-flow-arrow" aria-hidden />
        <div className="topo-flow-step topo-flow-step-accent">
          <span className="topo-flow-badge topo-flow-badge-tls" aria-hidden>
            TLS
          </span>
          <span className="topo-flow-label">
            HAProxy
            <span className="topo-flow-sublabel">cert-manager</span>
          </span>
        </div>
      </div>

      <div className="topo-preview-spine" aria-hidden />

      <div className="topo-preview-nodes">
        {PREVIEW_NODES.map((node) => (
          <article key={node.id} className={`topo-preview-node topo-preview-node-${node.accent}`}>
            <div className="topo-preview-node-head">
              <span className="topo-preview-dot" aria-hidden />
              <h4 className="topo-preview-title">{node.title}</h4>
            </div>
            <p className="topo-preview-summary">{node.summary}</p>
            {node.volumes ? (
              <p className="topo-preview-meta">
                <span className="topo-preview-volume-icon" aria-hidden>
                  ◐
                </span>
                {node.volumes}
              </p>
            ) : (
              <p className="topo-preview-meta muted">Ephemeral node disk</p>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}

function TopoDetailed({ compact }: { compact: boolean }) {
  return (
    <div className={`topo-detailed${compact ? " is-compact" : ""}`}>
      <div className="topo-ingress">
        <div className="topo-ingress-box">Hetzner Load Balancer</div>
        <div className="topo-connector" aria-hidden />
        <div className="topo-ingress-box topo-ingress-platform">
          HAProxy Ingress
          <span className="topo-ingress-sub">cert-manager (TLS)</span>
        </div>
      </div>
      <p className="topo-traffic-caption">
        Traffic: Internet → Hetzner LB → HAProxy → your hub domain (Let&apos;s Encrypt via cert-manager)
      </p>
      <div className="topo-nodes">
        <NodeCard
          title="Master"
          role="database"
          extra="k3s control-plane"
          workloads={MASTER_WORKLOADS}
          volumes={MASTER_VOLUMES}
        />
        <NodeCard
          title="Web worker"
          role="web"
          workloads={WEB_WORKLOADS}
          volumeNote="No persistent volumes on this node"
        />
        <NodeCard
          title="WebRTC worker"
          role="webrtc"
          workloads={WEBRTC_WORKLOADS}
          volumeNote="No persistent volumes on this node"
          footnote="Dialog & Coturn use hostNetwork for UDP/TCP ports"
        />
      </div>
    </div>
  );
}

/** Fixed architecture diagram — server types and volume sizes are wizard choices, not shown here. */
export function ClusterTopology({
  compact = false,
  preview = false,
  expandable = false,
  defaultExpanded = false,
}: ClusterTopologyProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailsId = useId();
  const showDetails = !preview || (expandable && expanded);

  if (preview) {
    return (
      <div className="cluster-topology cluster-topology-preview" aria-label="Cluster architecture">
        <div className="topo-preview-header">
          <div>
            <p className="topo-preview-eyebrow">Architecture</p>
            <p className="topo-preview-caption muted">
              Three Hetzner servers · k3s · HCCE workloads · Retain volumes on the master
            </p>
          </div>
          {expandable && (
            <button
              type="button"
              className="btn btn-outline btn-sm topo-expand-btn"
              aria-expanded={expanded}
              aria-controls={detailsId}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Hide details" : "Show details"}
            </button>
          )}
        </div>

        {!showDetails && <TopoPreview />}

        {expandable && (
          <div
            id={detailsId}
            className={`topo-details-panel${expanded ? " is-open" : ""}`}
            hidden={!expanded}
          >
            <TopoDetailed compact={compact} />
          </div>
        )}

        {!expandable && showDetails && <TopoDetailed compact={compact} />}
      </div>
    );
  }

  return (
    <div className={`cluster-topology${compact ? " is-compact" : ""}`} aria-label="Cluster architecture">
      <TopoDetailed compact={compact} />
    </div>
  );
}
