import { Link } from "react-router-dom";
import { ClusterTopology } from "../components/ClusterTopology";
import { SavedInstances } from "../components/SavedInstances";
import {
  LandingHeroBrand,
  InstallerTopBrand,
  ResourceLinkRow,
  HF_HOME,
  INSTALLER_GITHUB,
} from "../components/HubsFoundationLinks";

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-start">
          <InstallerTopBrand />
          <span className="topbar-divider" aria-hidden />
          <Link to="/" className="topbar-product">
            Hetzner Installer
          </Link>
        </div>
        <div className="topbar-end">
          <ResourceLinkRow className="topbar-hf-links" />
          <span className="topbar-meta">127.0.0.1 · v1.2</span>
        </div>
      </header>
      <main className="main">{children}</main>
      <footer className="app-footer">
        <InstallerTopBrand height={24} />
        <ResourceLinkRow />
        <span className="muted app-footer-meta">
          Local provisioning tool · not affiliated with hosting or the hoster ·{" "}
          <a href={`${INSTALLER_GITHUB}/blob/main/LICENSE`} target="_blank" rel="noopener noreferrer">
            MPL 2.0
          </a>
        </span>
      </footer>
    </div>
  );
}

export function LandingPage() {
  return (
    <div className="landing-wrap">
      <div className="landing-cluster">
        <LandingHeroBrand />
        <SavedInstances />
        <div className="landing-actions">
          <Link to="/instances/new" className="btn btn-outline" style={{ textDecoration: "none" }}>
            Create new instance
          </Link>
        </div>
        <div className="landing-topology">
          <ClusterTopology preview expandable />
        </div>
        <p className="landing-attribution muted">
          Hubs is open source software maintained by the{" "}
          <a href={HF_HOME} target="_blank" rel="noopener noreferrer">
            Hubs Foundation
          </a>
          .
        </p>
      </div>
    </div>
  );
}
