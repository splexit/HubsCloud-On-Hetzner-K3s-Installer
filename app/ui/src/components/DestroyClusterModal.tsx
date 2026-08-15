import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type DbBackupEntry = {
  filename: string;
  size_bytes: number;
  backup_date: string;
  backup_time: string;
  created_at: string;
};

type DbBackupsList = {
  available: boolean;
  message: string;
  recent: DbBackupEntry[];
  latest: DbBackupEntry | null;
};

type BackupChoice = "latest" | "date" | "filename";

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DestroyClusterModal({
  instanceId,
  open,
  backupsEnabled,
  onClose,
  onStarted,
}: {
  instanceId: string;
  open: boolean;
  backupsEnabled: boolean;
  onClose: () => void;
  onStarted: () => void;
}) {
  const [saveBundle, setSaveBundle] = useState(true);
  const [backupChoice, setBackupChoice] = useState<BackupChoice>("latest");
  const [backupDate, setBackupDate] = useState("");
  const [selectedFilename, setSelectedFilename] = useState("");
  const [backups, setBackups] = useState<DbBackupsList | null>(null);
  const [loadingBackups, setLoadingBackups] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const loadBackups = useCallback(async () => {
    if (!backupsEnabled) return;
    setLoadingBackups(true);
    try {
      const res = await api<DbBackupsList>(`/instances/${instanceId}/db-backups`);
      setBackups(res);
      if (res.latest && !selectedFilename) {
        setSelectedFilename(res.latest.filename);
      }
    } catch (e) {
      setBackups(null);
      setError(e instanceof Error ? e.message : "Could not load backups");
    } finally {
      setLoadingBackups(false);
    }
  }, [backupsEnabled, instanceId, selectedFilename]);

  useEffect(() => {
    if (!open) return;
    setError("");
    void loadBackups();
  }, [open, loadBackups]);

  if (!open) return null;

  const canPickBackup = saveBundle && backupsEnabled && backups?.available;

  const submit = async () => {
    setSubmitting(true);
    setError("");
    try {
      const body: Record<string, unknown> = {
        confirm: true,
        save_restore_bundle: saveBundle,
        backup_latest: saveBundle && backupChoice === "latest",
      };
      if (saveBundle && backupChoice === "date") {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(backupDate)) {
          setError("Enter a backup date as YYYY-MM-DD");
          setSubmitting(false);
          return;
        }
        body.backup_date = backupDate;
        body.backup_latest = false;
      }
      if (saveBundle && backupChoice === "filename") {
        if (!selectedFilename) {
          setError("Select a backup from the list");
          setSubmitting(false);
          return;
        }
        body.backup_filename = selectedFilename;
        body.backup_latest = false;
      }
      await api(`/instances/${instanceId}/destroy`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onStarted();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Destroy failed to start");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="destroy-title">
      <div className="modal-card" style={{ maxWidth: 560 }}>
        <h3 id="destroy-title">Destroy cluster infrastructure?</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Terraform destroy removes servers, private network, firewall, placement group, and SSH key. Kubernetes
          ingress load balancer(s) and CSI block volumes are kept on purpose so data is not destroyed accidentally.
        </p>

        <label style={{ display: "flex", gap: 10, alignItems: "flex-start", marginTop: 16 }}>
          <input
            type="checkbox"
            checked={saveBundle}
            onChange={(e) => setSaveBundle(e.target.checked)}
            style={{ marginTop: 4 }}
          />
          <span>
            <strong>Save local restore bundle</strong>
            <span className="muted" style={{ display: "block", fontSize: 13, marginTop: 4 }}>
              Copies secrets, spec, SSH keys, volume inventory, and a PostgreSQL dump into{" "}
              <code>restore-bundle/</code> so you can recreate without matching secrets manually.
            </span>
          </span>
        </label>

        {saveBundle && !backupsEnabled && (
          <p className="warn-text" style={{ fontSize: 13, marginTop: 12 }}>
            Cluster must be running to pull a database dump. Uncheck restore bundle or wait until provisioning
            succeeds.
          </p>
        )}

        {saveBundle && backupsEnabled && (
          <div className="destroy-backup-picker" style={{ marginTop: 16, paddingLeft: 28 }}>
            {loadingBackups && <p className="muted">Loading backups…</p>}
            {!loadingBackups && backups && !backups.available && (
              <p className="warn-text" style={{ fontSize: 13 }}>
                {backups.message || "Backups unavailable — destroy will fail if restore bundle is required."}
              </p>
            )}
            {canPickBackup && (
              <>
                <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
                  Choose which nightly dump to include:
                </p>
                <label style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                  <input
                    type="radio"
                    name="backup-choice"
                    checked={backupChoice === "latest"}
                    onChange={() => setBackupChoice("latest")}
                  />
                  Latest
                  {backups.latest ? (
                    <span className="muted" style={{ fontSize: 13 }}>
                      ({backups.latest.backup_date} {backups.latest.backup_time} UTC ·{" "}
                      {formatBytes(backups.latest.size_bytes)})
                    </span>
                  ) : null}
                </label>
                <label style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                  <input
                    type="radio"
                    name="backup-choice"
                    checked={backupChoice === "date"}
                    onChange={() => setBackupChoice("date")}
                  />
                  By date
                  <input
                    type="date"
                    value={backupDate}
                    disabled={backupChoice !== "date"}
                    onChange={(e) => setBackupDate(e.target.value)}
                    style={{ marginLeft: 8 }}
                  />
                </label>
                <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 8 }}>
                  <input
                    type="radio"
                    name="backup-choice"
                    checked={backupChoice === "filename"}
                    onChange={() => setBackupChoice("filename")}
                    style={{ marginTop: 6 }}
                  />
                  <span style={{ flex: 1 }}>
                    From last 7 days
                    <select
                      value={selectedFilename}
                      disabled={backupChoice !== "filename"}
                      onChange={(e) => setSelectedFilename(e.target.value)}
                      style={{ display: "block", width: "100%", marginTop: 6 }}
                    >
                      <option value="">Select backup…</option>
                      {backups.recent.map((entry) => (
                        <option key={entry.filename} value={entry.filename}>
                          {entry.backup_date} {entry.backup_time} UTC · {formatBytes(entry.size_bytes)}
                        </option>
                      ))}
                    </select>
                  </span>
                </label>
              </>
            )}
          </div>
        )}

        {error && <p className="bad-text" style={{ marginTop: 12 }}>{error}</p>}

        <div className="modal-actions">
          <button type="button" className="btn btn-outline" disabled={submitting} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary bad-text"
            style={{ borderColor: "var(--bad)" }}
            disabled={submitting || (saveBundle && (!backupsEnabled || !canPickBackup))}
            onClick={() => void submit()}
          >
            {submitting ? "Starting destroy…" : "Destroy infrastructure"}
          </button>
        </div>
      </div>
    </div>
  );
}
