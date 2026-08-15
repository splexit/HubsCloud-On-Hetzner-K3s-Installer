import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ClusterTopology } from "../components/ClusterTopology";

const STEPS = [
  "Credentials",
  "Domain & email",
  "Machines & SSH",
  "SMTP",
  "Container images",
  "Optional APIs",
  "Review",
];

const CORE_APPS = ["reticulum", "hubs", "spoke"] as const;
type CoreApp = (typeof CORE_APPS)[number];
type ImageMode = "default" | "pin" | "custom";

const DATA_VOLUME_GB = [10, 20, 50, 100, 200, 500] as const;
const BACKUP_VOLUME_GB = [10, 20] as const;

const PVC_LABELS: Record<string, string> = {
  "pgsql-pvc": "PostgreSQL database",
  "ret-pvc": "Reticulum (assets & uploads)",
  "pgsql-backups-pvc": "PostgreSQL backup",
};

type VolumeReattachPolicy = {
  mode: "provision_new" | "reattach_saved";
  volumes: Record<string, boolean>;
};

type VolumeContextEntry = {
  pvc_name: string;
  role: string;
  size: string;
  hetzner_volume_id: number | null;
  hetzner_volume_name: string | null;
  status: string;
  in_hetzner: boolean;
  in_inventory: boolean;
  selected_for_reattach: boolean;
  reattach_eligible: boolean;
};

type VolumesContext = {
  reattach_eligible: boolean;
  secrets_available: boolean;
  entries: VolumeContextEntry[];
};

function getReattachPolicy(spec: Spec): VolumeReattachPolicy {
  const raw = spec.volume_reattach as VolumeReattachPolicy | undefined;
  if (raw && (raw.mode === "provision_new" || raw.mode === "reattach_saved")) {
    return { mode: raw.mode, volumes: { ...(raw.volumes || {}) } };
  }
  return { mode: "provision_new", volumes: {} };
}

type RestoreFromBundlePolicy = {
  enabled: boolean;
};

type RestoreBundleStatus = {
  present: boolean;
  manifest: {
    db_backup_filename: string;
    db_backup_date: string;
    db_backup_time: string;
  } | null;
};

function getRestoreFromBundle(spec: Spec): RestoreFromBundlePolicy {
  const raw = spec.restore_from_bundle as RestoreFromBundlePolicy | undefined;
  return { enabled: Boolean(raw?.enabled) };
}

function volumeGi(gb: number) {
  return `${gb}Gi`;
}

function volumeGbFromGi(gi: string | undefined, fallback: number) {
  const m = String(gi || "").match(/^(\d+)Gi$/);
  return m ? Number(m[1]) : fallback;
}

function formatVolumeLabel(gb: number) {
  return `${gb} GB`;
}

type AppImageCfg = { mode: ImageMode; tag: string; image: string };

const APP_DEFAULTS: Record<CoreApp, { repo: string; tag: string; label: string }> = {
  reticulum: { repo: "hubsfoundation/reticulum", tag: "stable-latest", label: "Reticulum" },
  hubs: { repo: "hubsfoundation/hubs", tag: "stable-latest", label: "Hubs" },
  spoke: { repo: "hubsfoundation/spoke", tag: "stable-latest", label: "Spoke" },
};

const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const IMAGE_RE = /^[a-z0-9]([a-z0-9._-]*(\/[a-z0-9][a-z0-9._-]*)*)?(:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|@sha256:[a-f0-9]{64})?$/i;

function getAppImageCfg(spec: Spec, app: CoreApp): AppImageCfg {
  const all = (spec.core_app_images as Record<string, AppImageCfg> | undefined) || {};
  const cfg = all[app];
  const mode = cfg?.mode === "pin" || cfg?.mode === "custom" ? cfg.mode : "default";
  return { mode, tag: cfg?.tag || "", image: cfg?.image || "" };
}

function resolveAppImagePreview(app: CoreApp, cfg: AppImageCfg): string {
  const meta = APP_DEFAULTS[app];
  if (cfg.mode === "custom" && cfg.image.trim()) return cfg.image.trim();
  if (cfg.mode === "pin" && cfg.tag.trim()) return `${meta.repo}:${cfg.tag.trim()}`;
  return `${meta.repo}:${meta.tag}`;
}

function appImageValid(cfg: AppImageCfg): boolean {
  if (cfg.mode === "default") return true;
  if (cfg.mode === "pin") return Boolean(cfg.tag.trim()) && TAG_RE.test(cfg.tag.trim());
  if (cfg.mode === "custom") {
    const image = cfg.image.trim();
    return Boolean(image) && !image.includes(" ") && IMAGE_RE.test(image);
  }
  return false;
}

type Spec = Record<string, unknown>;

type Location = { name: string; description: string; city: string; country: string };

type ServerType = {
  name: string;
  description: string;
  cores: number;
  memory: number;
  disk: number;
  category: string;
  available: boolean;
  recommended: boolean;
  price_monthly_gross: string | null;
};

type RoleSpec = { cores: number; memory_gb: number };
type SizeRecommendations = Record<string, { master: RoleSpec; web: RoleSpec; webrtc: RoleSpec }>;

function formatRoleSpec(spec: RoleSpec) {
  return `${spec.cores} vCPU, ${spec.memory_gb} GB RAM`;
}

function formatSizeRecommendation(size: string, rec: SizeRecommendations[string]) {
  return `Master ${formatRoleSpec(rec.master)} · Web worker ${formatRoleSpec(rec.web)} · WebRTC worker ${formatRoleSpec(rec.webrtc)}`;
}

function formatTypeLabel(t: ServerType) {
  const price = t.price_monthly_gross ? ` · €${t.price_monthly_gross}/mo` : "";
  return `${t.name} — ${t.cores} vCPU, ${t.memory} GB RAM, ${t.disk} GB${price}`;
}

export function WizardPage({ instanceId }: { instanceId: string }) {
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const recreateIntent = searchParams.get("recreate") === "1";
  const initialStep = recreateIntent
    ? STEPS.length - 1
    : Math.min(Math.max(Number(searchParams.get("step") || "0"), 0), STEPS.length - 1);
  const [step, setStep] = useState(initialStep);
  const [spec, setSpec] = useState<Spec>({});
  const specRef = useRef<Spec>({});
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoReattachAppliedRef = useRef(false);
  const autoRestoreBundleAppliedRef = useRef(false);
  const [tokenOk, setTokenOk] = useState<boolean | null>(null);
  const [tokenMsg, setTokenMsg] = useState("");
  const [tokenValidating, setTokenValidating] = useState(false);
  const [saved, setSaved] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const [locations, setLocations] = useState<Location[]>([]);
  const [serverTypes, setServerTypes] = useState<ServerType[]>([]);
  const [sizeRecommendations, setSizeRecommendations] = useState<SizeRecommendations>({});
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [serverTypesOk, setServerTypesOk] = useState<boolean | null>(null);
  const [serverTypesMsg, setServerTypesMsg] = useState("");
  const [volumesCtx, setVolumesCtx] = useState<VolumesContext | null>(null);
  const [volumesLoading, setVolumesLoading] = useState(false);
  const [restoreBundle, setRestoreBundle] = useState<RestoreBundleStatus | null>(null);

  useEffect(() => {
    api<{ spec: Spec }>(`/instances/${instanceId}`).then((d) => {
      setSpec(d.spec);
      specRef.current = d.spec;
    });
  }, [instanceId]);

  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  const location = (spec.location as string) || "";

  const loadLocations = useCallback(async () => {
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const [locRes, recRes] = await Promise.all([
        api<{ locations: Location[] }>(`/instances/${instanceId}/hetzner/locations`),
        api<{ recommendations: SizeRecommendations }>(`/instances/${instanceId}/hetzner/cluster-size-recommendations`),
      ]);
      setLocations(locRes.locations);
      setSizeRecommendations(recRes.recommendations);
    } catch (e) {
      setCatalogError(e instanceof Error ? e.message : "Could not load Hetzner locations");
    } finally {
      setCatalogLoading(false);
    }
  }, [instanceId]);

  const loadServerTypes = useCallback(async () => {
    if (!location) {
      setServerTypes([]);
      return;
    }
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const typesRes = await api<{ server_types: ServerType[] }>(
        `/instances/${instanceId}/hetzner/server-types?location=${encodeURIComponent(location)}`
      );
      setServerTypes(typesRes.server_types);
      setServerTypesOk(null);
      setServerTypesMsg("");
    } catch (e) {
      setCatalogError(e instanceof Error ? e.message : "Could not load server types for this location");
      setServerTypes([]);
    } finally {
      setCatalogLoading(false);
    }
  }, [instanceId, location]);

  useEffect(() => {
    if (step === 2 && tokenOk === true) {
      void loadLocations();
    }
  }, [step, tokenOk, loadLocations]);

  useEffect(() => {
    if (step === 2 && tokenOk === true && location) {
      void loadServerTypes();
    }
  }, [step, tokenOk, location, loadServerTypes]);

  const loadVolumes = useCallback(async () => {
    setVolumesLoading(true);
    try {
      const ctx = await api<VolumesContext>(`/instances/${instanceId}/volumes`);
      setVolumesCtx(ctx);
    } catch {
      setVolumesCtx(null);
    } finally {
      setVolumesLoading(false);
    }
  }, [instanceId]);

  useEffect(() => {
    if ((step === 2 || step === 6) && (tokenOk === true || recreateIntent)) {
      void loadVolumes();
    }
  }, [step, tokenOk, loadVolumes, recreateIntent]);

  useEffect(() => {
    if (!recreateIntent || tokenOk !== null) return;
    const token = String(spec.hetzner_api_token || "").trim();
    if (!token) return;
    void validateToken();
  }, [recreateIntent, spec.hetzner_api_token, tokenOk]);

  useEffect(() => {
    if (!volumesCtx?.reattach_eligible || autoReattachAppliedRef.current) return;
    const policy = getReattachPolicy(specRef.current);
    if (policy.mode !== "provision_new") return;
    if (!volumesCtx.secrets_available) return;
    autoReattachAppliedRef.current = true;
    patchAndSave({
      volume_reattach: {
        mode: "reattach_saved",
        volumes: policy.volumes,
      },
      restore_from_bundle: { enabled: false },
    });
  }, [volumesCtx]);

  useEffect(() => {
    void api<RestoreBundleStatus>(`/instances/${instanceId}/restore-bundle`)
      .then(setRestoreBundle)
      .catch(() => setRestoreBundle(null));
  }, [instanceId]);

  useEffect(() => {
    if (!restoreBundle?.present) return;
    if (volumesCtx?.reattach_eligible && volumesCtx.secrets_available) return;
    const policy = getRestoreFromBundle(specRef.current);
    if (policy.enabled || autoRestoreBundleAppliedRef.current) return;
    autoRestoreBundleAppliedRef.current = true;
    patchAndSave({ restore_from_bundle: { enabled: true } });
  }, [restoreBundle, volumesCtx]);

  const reattachPolicy = getReattachPolicy(spec);
  const restoreFromBundle = getRestoreFromBundle(spec);
  const isRecreateFlow =
    recreateIntent ||
    (volumesCtx?.reattach_eligible === true && reattachPolicy.mode === "reattach_saved");
  const reattachBlocked =
    reattachPolicy.mode === "reattach_saved" && volumesCtx?.secrets_available === false;
  const restoreBundleBlocked =
    restoreFromBundle.enabled && !restoreBundle?.present;
  const startBlocked = reattachBlocked || restoreBundleBlocked;

  const setReattachMode = (enabled: boolean) => {
    patchAndSave({
      volume_reattach: {
        mode: enabled ? "reattach_saved" : "provision_new",
        volumes: reattachPolicy.volumes,
      },
      ...(enabled ? { restore_from_bundle: { enabled: false } } : {}),
    });
  };

  const setRestoreFromBundleEnabled = (enabled: boolean) => {
    patchAndSave({
      restore_from_bundle: { enabled },
      ...(enabled
        ? {
            volume_reattach: {
              mode: "provision_new",
              volumes: reattachPolicy.volumes,
            },
          }
        : {}),
    });
  };

  const setReattachVolume = (pvcName: string, enabled: boolean) => {
    patchAndSave({
      volume_reattach: {
        mode: reattachPolicy.mode,
        volumes: { ...reattachPolicy.volumes, [pvcName]: enabled },
      },
    });
  };

  const isVolumeSizeLocked = (pvcName: string) => {
    if (reattachPolicy.mode !== "reattach_saved") return false;
    const entry = volumesCtx?.entries.find((e) => e.pvc_name === pvcName);
    if (!entry?.reattach_eligible) return false;
    const selected =
      pvcName in reattachPolicy.volumes ? reattachPolicy.volumes[pvcName] : true;
    return selected;
  };

  useEffect(() => {
    if (recreateIntent && step === STEPS.length - 1) return;
    if (step > 0 && tokenOk !== true) {
      setStep(0);
    }
  }, [step, tokenOk, recreateIntent]);

  const saveSpec = useCallback(
    async (next?: Spec) => {
      const payload = next ?? specRef.current;
      specRef.current = payload;
      setSpec(payload);
      await api(`/instances/${instanceId}/spec`, {
        method: "PUT",
        body: JSON.stringify({ spec: payload }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    },
    [instanceId]
  );

  const selectClusterSize = (size: "small" | "medium" | "large") => {
    patchAndSave({ cluster_size: size });
    setServerTypesOk(null);
  };

  const resolvedTypes = useMemo(
    () => ({
      master: (spec.master_server_type as string) || "",
      web: (spec.worker_server_type as string) || "",
      webrtc: (spec.webrtc_server_type as string) || "",
    }),
    [spec.master_server_type, spec.worker_server_type, spec.webrtc_server_type]
  );

  const patchLocal = (patch: Spec) => {
    setSpec((prev) => {
      const next = { ...prev, ...patch };
      specRef.current = next;
      return next;
    });
  };

  const scheduleBlurSave = () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      saveTimerRef.current = null;
      void saveSpec();
    }, 400);
  };

  const patchAndSave = (patch: Spec) => {
    const next = { ...specRef.current, ...patch };
    void saveSpec(next);
  };

  const patchAppImage = (app: CoreApp, patch: Partial<AppImageCfg>) => {
    const all = { ...((specRef.current.core_app_images as Record<string, AppImageCfg>) || {}) };
    all[app] = { ...getAppImageCfg(specRef.current, app), ...patch };
    patchLocal({ core_app_images: all });
  };

  const saveAppImage = (app: CoreApp, patch: Partial<AppImageCfg>) => {
    const all = { ...((specRef.current.core_app_images as Record<string, AppImageCfg>) || {}) };
    all[app] = { ...getAppImageCfg(specRef.current, app), ...patch };
    patchAndSave({ core_app_images: all });
  };

  const validateToken = async (): Promise<boolean> => {
    const token = String(specRef.current.hetzner_api_token || "").trim();
    if (!token) {
      setTokenOk(false);
      setTokenMsg("Enter your Hetzner Cloud API token first.");
      return false;
    }
    setTokenValidating(true);
    try {
      await saveSpec();
      const r = await api<{ ok: boolean; message: string }>(`/instances/${instanceId}/validate-hetzner`, { method: "POST" });
      setTokenOk(r.ok);
      setTokenMsg(r.message);
      return r.ok;
    } catch (e) {
      setTokenOk(false);
      setTokenMsg(e instanceof Error ? e.message : "Token validation failed");
      return false;
    } finally {
      setTokenValidating(false);
    }
  };

  const validateServerTypes = async () => {
    await saveSpec();
    const r = await api<{ ok: boolean; message: string }>(`/instances/${instanceId}/validate-server-types`, { method: "POST" });
    setServerTypesOk(r.ok);
    setServerTypesMsg(r.message);
    return r.ok;
  };

  const generateSsh = async () => {
    const r = await api<{ public_key: string }>(`/instances/${instanceId}/ssh/generate`, { method: "POST" });
    patchAndSave({ ssh_public_key: r.public_key, ssh_key_generated: true });
  };

  const goNext = async () => {
    await saveSpec();
    if (step === 0) {
      const ok = await validateToken();
      if (!ok) return;
    }
    if (step === 2) {
      const ok = await validateServerTypes();
      if (!ok) return;
    }
    setStep((s) => s + 1);
  };

  const canContinue = useMemo(() => {
    switch (step) {
      case 0:
        return Boolean(String(spec.hetzner_api_token || "").trim()) && tokenOk === true && !tokenValidating;
      case 1:
        return Boolean(spec.hub_domain && spec.admin_email);
      case 2:
        return Boolean(
          spec.ssh_public_key &&
            spec.ssh_key_generated &&
            spec.location &&
            resolvedTypes.master &&
            resolvedTypes.web &&
            resolvedTypes.webrtc &&
            serverTypes.length > 0
        );
      case 3:
        return Boolean(
          spec.smtp_host && spec.smtp_user && spec.smtp_password && spec.smtp_port && spec.smtp_from
        );
      case 4:
        return CORE_APPS.every((app) => appImageValid(getAppImageCfg(spec, app)));
      default:
        return true;
    }
  }, [step, spec, tokenOk, tokenValidating, resolvedTypes, serverTypes.length]);

  const startCreate = async () => {
    if (startBlocked) return;
    await saveSpec();
    await api(`/instances/${instanceId}/create`, { method: "POST" });
    nav(`/instances/${instanceId}`);
  };

  const locationOptions = locations.length
    ? locations
    : [];

  const activeSize = ((spec.cluster_size as string) || "medium") as "small" | "medium" | "large";
  const activeRecommendation = sizeRecommendations[activeSize];

  return (
    <div className="wizard-workspace">
    <div className="wizard-grid">
      <nav aria-label="Setup steps" className="step-rail-wrap">
        <ol className="step-rail">
          {STEPS.map((label, i) => {
            const done = i < step;
            const current = i === step;
            return (
              <li key={label} className="step-rail-item">
                <button
                  type="button"
                  className={`step-rail-btn${current ? " is-current" : ""}${done ? " is-done" : ""}`}
                  disabled={!done}
                  onClick={() => done && setStep(i)}
                >
                  <span className="step-rail-num">{done ? "✓" : i + 1}</span>
                  {label}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <section className="step-enter wizard-form-card">
        {step === 0 && (
          <>
            <h2>Credentials</h2>
            <p className="muted">Your Hetzner Cloud API token. Validate it before continuing.</p>
            <div className="field">
              <label htmlFor="token">API token</label>
              <input
                id="token"
                type="password"
                value={(spec.hetzner_api_token as string) || ""}
                onChange={(e) => {
                  patchLocal({ hetzner_api_token: e.target.value });
                  setTokenOk(null);
                  setTokenMsg("");
                }}
                onBlur={scheduleBlurSave}
              />
            </div>
            <button
              type="button"
              className="btn btn-outline"
              disabled={tokenValidating || !String(spec.hetzner_api_token || "").trim()}
              onClick={() => void validateToken()}
            >
              {tokenValidating ? "Validating…" : "Validate token"}
            </button>
            {tokenOk === false && (
              <p className="bad-text validate-in" style={{ marginTop: 12 }}>
                {tokenMsg || "Token invalid — fix it and validate again before continuing."}
              </p>
            )}
            {tokenOk === true && (
              <p className="ok-text validate-in" style={{ marginTop: 12 }}>
                {tokenMsg || "Token valid"}
              </p>
            )}
          </>
        )}

        {step === 1 && (
          <>
            <h2>Domain & email</h2>
            <div className="field">
              <label htmlFor="hub">Hub domain</label>
              <input
                id="hub"
                value={(spec.hub_domain as string) || ""}
                onChange={(e) => patchLocal({ hub_domain: e.target.value })}
                onBlur={scheduleBlurSave}
                placeholder="hubs.example.com"
              />
            </div>
            <div className="field">
              <label htmlFor="email">Admin email</label>
              <input
                id="email"
                type="email"
                value={(spec.admin_email as string) || ""}
                onChange={(e) => patchLocal({ admin_email: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <h2>Machines & SSH</h2>
            <p className="muted">Server types are loaded live from Hetzner for your region.</p>

            <div className="field">
              <label htmlFor="loc">Location</label>
              <select
                id="loc"
                value={location}
                onChange={(e) => {
                  patchAndSave({
                    location: e.target.value,
                    master_server_type: "",
                    worker_server_type: "",
                    webrtc_server_type: "",
                  });
                  setServerTypesOk(null);
                }}
              >
                <option value="" disabled>
                  Choose location…
                </option>
                {locationOptions.map((l) => (
                  <option key={l.name} value={l.name}>
                    {l.name}
                    {l.city ? ` — ${l.city}` : ""}
                  </option>
                ))}
              </select>
            </div>

            {!location && !catalogLoading && (
              <p className="muted">Choose a location to load available server types for that region.</p>
            )}

            {catalogLoading && <p className="muted">Loading from Hetzner…</p>}
            {catalogError && (
              <div className="panel bad-text" style={{ marginBottom: 12 }}>
                {catalogError}
                <button type="button" className="btn btn-outline" style={{ marginTop: 8 }} onClick={() => { void loadLocations(); if (location) void loadServerTypes(); }}>
                  Retry
                </button>
              </div>
            )}

            <div className="field">
              <label>Cluster size guidance</label>
              <div className="segmented">
                {(["small", "medium", "large"] as const).map((s) => (
                  <button
                    key={s}
                    type="button"
                    className={`segmented-btn${spec.cluster_size === s ? " is-active" : ""}`}
                    onClick={() => selectClusterSize(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
              {activeRecommendation && (
                <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
                  Recommended resources ({activeSize}): {formatSizeRecommendation(activeSize, activeRecommendation)}
                </p>
              )}
              <p className="muted" style={{ marginTop: 8, fontSize: 13, marginBottom: 0 }}>
                Pick matching server types below — names differ by location.
              </p>
            </div>

            {location && serverTypes.length > 0 && (
              <>
                <div className="field">
                  <label htmlFor="master-type">Master (database) server type</label>
                  <select
                    id="master-type"
                    value={resolvedTypes.master}
                    onChange={(e) => {
                      patchAndSave({ master_server_type: e.target.value });
                      setServerTypesOk(null);
                    }}
                  >
                    <option value="" disabled>
                      Choose master server type…
                    </option>
                    {serverTypes.map((t) => (
                      <option key={t.name} value={t.name}>
                        {formatTypeLabel(t)}
                        {t.recommended ? " ★" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="web-type">Web worker server type</label>
                  <select
                    id="web-type"
                    value={resolvedTypes.web}
                    onChange={(e) => {
                      patchAndSave({ worker_server_type: e.target.value });
                      setServerTypesOk(null);
                    }}
                  >
                    <option value="" disabled>
                      Choose web worker server type…
                    </option>
                    {serverTypes.map((t) => (
                      <option key={t.name} value={t.name}>
                        {formatTypeLabel(t)}
                        {t.recommended ? " ★" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="webrtc-type">WebRTC worker server type</label>
                  <select
                    id="webrtc-type"
                    value={resolvedTypes.webrtc}
                    onChange={(e) => {
                      patchAndSave({ webrtc_server_type: e.target.value });
                      setServerTypesOk(null);
                    }}
                  >
                    <option value="" disabled>
                      Choose WebRTC worker server type…
                    </option>
                    {serverTypes.map((t) => (
                      <option key={t.name} value={t.name}>
                        {formatTypeLabel(t)}
                        {t.recommended ? " ★" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <button type="button" className="btn btn-outline" onClick={() => void validateServerTypes()}>
                  Validate server types
                </button>
                {serverTypesOk !== null && (
                  <p className={serverTypesOk ? "ok-text" : "bad-text"} style={{ marginTop: 12 }}>
                    {serverTypesMsg}
                  </p>
                )}
              </>
            )}

            <div className="cluster" style={{ marginTop: 16 }}>
              <p className="cluster-title">Storage volumes</p>
              {volumesCtx?.reattach_eligible && (
                <div className="cluster" style={{ marginBottom: 16, background: "var(--bg-subtle)" }}>
                  <p className="cluster-title" style={{ marginTop: 0 }}>
                    Existing Hetzner volumes
                  </p>
                  <p className="muted" style={{ marginTop: 0 }}>
                    Saved volume IDs from a previous run of this instance were found. Reattach them instead of
                    provisioning empty disks.
                  </p>
                  {!volumesCtx.secrets_available && (
                    <p className="bad-text" style={{ marginTop: 0 }}>
                      Saved secrets are missing — reattach requires the same secrets bundle from the original
                      provision. Import it or use the same instance data before recreating.
                    </p>
                  )}
                  <label className="field" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                    <input
                      type="checkbox"
                      checked={reattachPolicy.mode === "reattach_saved"}
                      disabled={!volumesCtx.secrets_available}
                      onChange={(e) => setReattachMode(e.target.checked)}
                    />
                    <span>Reattach saved volumes</span>
                  </label>
                  {volumesLoading && <p className="muted">Checking saved volumes…</p>}
                  <ul className="topo-list" style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
                    {(volumesCtx.entries || [])
                      .filter((e) => e.reattach_eligible)
                      .map((entry) => {
                        const checked =
                          reattachPolicy.mode === "reattach_saved" &&
                          (entry.pvc_name in reattachPolicy.volumes
                            ? reattachPolicy.volumes[entry.pvc_name]
                            : true);
                        return (
                          <li key={entry.pvc_name} style={{ marginBottom: 8 }}>
                            <label style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={reattachPolicy.mode !== "reattach_saved"}
                                onChange={(e) => setReattachVolume(entry.pvc_name, e.target.checked)}
                              />
                              <span>
                                <strong>{PVC_LABELS[entry.pvc_name] || entry.pvc_name}</strong>
                                <br />
                                <span className="muted" style={{ fontSize: 13 }}>
                                  Hetzner #{entry.hetzner_volume_id} · {entry.size} · {entry.status}
                                </span>
                              </span>
                            </label>
                          </li>
                        );
                      })}
                  </ul>
                </div>
              )}
              <p className="muted" style={{ marginTop: 0 }}>
                Hetzner Cloud block volumes — minimum 10 GB. Sizes bill continuously until deleted.
              </p>
              <div className="field">
                <label htmlFor="pgsql-volume">PostgreSQL database</label>
                <select
                  id="pgsql-volume"
                  value={volumeGbFromGi(spec.pgsql_volume_size as string, 10)}
                  disabled={isVolumeSizeLocked("pgsql-pvc")}
                  onChange={(e) => patchAndSave({ pgsql_volume_size: volumeGi(Number(e.target.value)) })}
                >
                  {DATA_VOLUME_GB.map((gb) => (
                    <option key={gb} value={gb}>
                      {formatVolumeLabel(gb)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="reticulum-volume">Reticulum (assets & uploads)</label>
                <select
                  id="reticulum-volume"
                  value={volumeGbFromGi(spec.reticulum_volume_size as string, 10)}
                  disabled={isVolumeSizeLocked("ret-pvc")}
                  onChange={(e) => patchAndSave({ reticulum_volume_size: volumeGi(Number(e.target.value)) })}
                >
                  {DATA_VOLUME_GB.map((gb) => (
                    <option key={gb} value={gb}>
                      {formatVolumeLabel(gb)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label htmlFor="pgsql-backup-volume">PostgreSQL backup</label>
                <select
                  id="pgsql-backup-volume"
                  value={volumeGbFromGi(spec.pgsql_backup_volume_size as string, 10)}
                  disabled={isVolumeSizeLocked("pgsql-backups-pvc")}
                  onChange={(e) => patchAndSave({ pgsql_backup_volume_size: volumeGi(Number(e.target.value)) })}
                >
                  {BACKUP_VOLUME_GB.map((gb) => (
                    <option key={gb} value={gb}>
                      {formatVolumeLabel(gb)}
                    </option>
                  ))}
                </select>
                <p className="muted" style={{ marginTop: 8, marginBottom: 0, fontSize: 13 }}>
                  Used only by the database backup CronJob — not the live Postgres data volume.
                </p>
              </div>
            </div>

            <div className="cluster" style={{ marginTop: 16 }}>
              <p className="cluster-title">SSH access</p>
              {!spec.ssh_key_generated ? (
                <>
                  <p className="muted" style={{ marginTop: 0 }}>
                    The installer generates an Ed25519 key pair and keeps the private key for kubeconfig fetch and node
                    hardening.
                  </p>
                  <button type="button" className="btn btn-outline" onClick={() => void generateSsh()}>
                    Generate key pair
                  </button>
                </>
              ) : (
                <>
                  <p className="ok-text" style={{ marginTop: 0, marginBottom: 8 }}>Key pair generated</p>
                  <div className="ssh-key-preview" aria-readonly="true">
                    {(spec.ssh_public_key as string) || ""}
                  </div>
                  <button type="button" className="btn btn-ghost" style={{ marginTop: 8 }} onClick={() => void generateSsh()}>
                    Regenerate key pair
                  </button>
                  <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
                    Regenerating replaces the key before provisioning starts.
                  </p>
                </>
              )}
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h2>SMTP</h2>
            <div className="field">
              <label htmlFor="smtp-host">Host</label>
              <input
                id="smtp-host"
                value={(spec.smtp_host as string) || ""}
                onChange={(e) => patchLocal({ smtp_host: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
            <div className="field">
              <label htmlFor="smtp-port">Port</label>
              <input
                id="smtp-port"
                type="number"
                value={(spec.smtp_port as number) || 587}
                placeholder="587"
                onChange={(e) => patchLocal({ smtp_port: Number(e.target.value) })}
                onBlur={scheduleBlurSave}
              />
              <p className="muted" style={{ marginTop: 6, marginBottom: 0 }}>
                Default: <strong>587</strong> (STARTTLS). Hetzner blocks outbound SMTP on{" "}
                <strong>465</strong> and <strong>22</strong> — do not use those. Port{" "}
                <strong>2020</strong> may work with some providers; prefer 587 when available.
              </p>
            </div>
            <div className="field">
              <label htmlFor="smtp-user">User</label>
              <input
                id="smtp-user"
                value={(spec.smtp_user as string) || ""}
                onChange={(e) => patchLocal({ smtp_user: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
            <div className="field">
              <label htmlFor="smtp-from">Sender email</label>
              <input
                id="smtp-from"
                type="email"
                value={(spec.smtp_from as string) || ""}
                onChange={(e) => patchLocal({ smtp_from: e.target.value })}
                onBlur={scheduleBlurSave}
                placeholder="you@yourdomain.com"
              />
              <p className="muted" style={{ marginTop: 6, marginBottom: 0 }}>
                From address on outgoing mail. Must be a mailbox your SMTP provider allows (often your login
                email, not <code>noreply@hub-domain</code>).
              </p>
            </div>
            <div className="field">
              <label htmlFor="smtp-pass">Password</label>
              <input
                id="smtp-pass"
                type="password"
                value={(spec.smtp_password as string) || ""}
                onChange={(e) => patchLocal({ smtp_password: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
          </>
        )}

        {step === 4 && (
          <>
            <h2>Container images</h2>
            <p className="muted">
              Choose Hubs Foundation defaults, pin a tag, or use your own public image. Private registries
              (password-protected GHCR, Docker Hub, etc.) are not supported yet.
            </p>
            <div className="cluster">
              <p className="cluster-title">Core apps</p>
            {CORE_APPS.map((app) => {
              const cfg = getAppImageCfg(spec, app);
              const meta = APP_DEFAULTS[app];
              return (
                <div key={app} className="app-image-block">
                  <h3 style={{ fontSize: 16, margin: "0 0 12px" }}>{meta.label}</h3>
                  <div className="image-options">
                    <label className="image-option">
                      <input
                        type="radio"
                        name={`${app}-mode`}
                        checked={cfg.mode === "default"}
                        onChange={() => saveAppImage(app, { mode: "default", tag: "", image: "" })}
                      />
                      <div className="image-option-body">
                        <div className="image-option-title">Hubs Foundation default</div>
                        <div className="image-option-desc">
                          {meta.repo}:{meta.tag}
                        </div>
                      </div>
                    </label>
                    <label className="image-option">
                      <input
                        type="radio"
                        name={`${app}-mode`}
                        checked={cfg.mode === "pin"}
                        onChange={() => patchAppImage(app, { mode: "pin", image: "" })}
                      />
                      <div className="image-option-body">
                        <div className="image-option-title">Pin a version tag</div>
                        <div className="image-option-desc">Same repository, specific tag on Hubs Foundation.</div>
                        {cfg.mode === "pin" && app === "reticulum" && (
                          <div className="image-option-desc" style={{ marginTop: 8, color: "#b45309" }}>
                            Use <strong>stable-latest</strong> or build <strong>852</strong> or higher
                            (e.g. <code>855</code>, <code>stable-855</code>). Reticulum switched email
                            from Bamboo to Swoosh on 2026-02-27 — builds up to 851 expect a different
                            config key and cannot send login emails.
                          </div>
                        )}
                        {cfg.mode === "pin" && (
                          <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                            <input
                              id={`${app}-tag`}
                              value={cfg.tag}
                              placeholder="stable-latest"
                              onChange={(e) => patchAppImage(app, { tag: e.target.value })}
                              onBlur={() => void saveSpec()}
                            />
                          </div>
                        )}
                      </div>
                    </label>
                    <label className="image-option">
                      <input
                        type="radio"
                        name={`${app}-mode`}
                        checked={cfg.mode === "custom"}
                        onChange={() => patchAppImage(app, { mode: "custom", tag: "" })}
                      />
                      <div className="image-option-body">
                        <div className="image-option-title">Custom public image</div>
                        <div className="image-option-desc">Full image reference, e.g. ghcr.io/org/hubs:1.0</div>
                        {cfg.mode === "custom" && app === "reticulum" && (
                          <div className="image-option-desc" style={{ marginTop: 8, color: "#b45309" }}>
                            Must be Reticulum build <strong>852</strong> or higher (Swoosh email,
                            since 2026-02-27). Builds up to 851 cannot send login emails with this
                            configuration.
                          </div>
                        )}
                        {cfg.mode === "custom" && (
                          <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                            <input
                              id={`${app}-image`}
                              value={cfg.image}
                              placeholder={`my-registry/${app}:latest`}
                              onChange={(e) => patchAppImage(app, { image: e.target.value })}
                              onBlur={() => void saveSpec()}
                            />
                          </div>
                        )}
                      </div>
                    </label>
                  </div>
                  <div className="image-preview" aria-live="polite">
                    Deploys as: {resolveAppImagePreview(app, cfg)}
                  </div>
                </div>
              );
            })}
            </div>
          </>
        )}

        {step === 5 && (
          <>
            <h2>Optional APIs</h2>
            <div className="field">
              <label>Sketchfab API key</label>
              <input
                value={(spec.sketchfab_api_key as string) || ""}
                onChange={(e) => patchLocal({ sketchfab_api_key: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
            <div className="field">
              <label>Tenor API key</label>
              <input
                value={(spec.tenor_api_key as string) || ""}
                onChange={(e) => patchLocal({ tenor_api_key: e.target.value })}
                onBlur={scheduleBlurSave}
              />
            </div>
          </>
        )}

        {step === 6 && (
          <>
            <h2>{isRecreateFlow ? "Review & recreate" : "Review"}</h2>
            {isRecreateFlow && (
              <p className="muted" style={{ marginTop: 0 }}>
                {reattachPolicy.mode === "reattach_saved"
                  ? "Infrastructure was destroyed; saved secrets and Hetzner block volumes will be reused. A new load balancer and servers will be created — update DNS if the LB IP changes."
                  : restoreFromBundle.enabled
                    ? "Infrastructure was destroyed; a local restore bundle will provision new volumes and restore the PostgreSQL database after HCCE deploys."
                    : "Infrastructure was destroyed; saved configuration will be reused with new Hetzner volumes."}
              </p>
            )}
            <div className="review-topology">
              <ClusterTopology />
            </div>
            <div className="review-grid">
              <div className="cluster">
                <p className="cluster-title">Infrastructure</p>
                <p><strong>Domain:</strong> {String(spec.hub_domain || "—")}</p>
                <p><strong>Location:</strong> {String(spec.location || "—")}</p>
                <p><strong>Size guidance:</strong> {String(spec.cluster_size || "medium")}</p>
                <p><strong>Master:</strong> {resolvedTypes.master || "—"}</p>
                <p><strong>Web:</strong> {resolvedTypes.web || "—"}</p>
                <p><strong>WebRTC:</strong> {resolvedTypes.webrtc || "—"}</p>
                <p><strong>PostgreSQL volume:</strong> {formatVolumeLabel(volumeGbFromGi(spec.pgsql_volume_size as string, 10))}</p>
                <p><strong>Reticulum volume:</strong> {formatVolumeLabel(volumeGbFromGi(spec.reticulum_volume_size as string, 10))}</p>
                <p><strong>Postgres backup volume:</strong> {formatVolumeLabel(volumeGbFromGi(spec.pgsql_backup_volume_size as string, 10))}</p>
                <p>
                  <strong>Volume strategy:</strong>{" "}
                  {reattachPolicy.mode === "reattach_saved"
                    ? `Reattach saved Hetzner volumes (${(volumesCtx?.entries || [])
                        .filter((e) => e.selected_for_reattach)
                        .map((e) => e.pvc_name)
                        .join(", ") || "none selected"})`
                    : restoreFromBundle.enabled
                      ? "Provision new volumes + restore database from local bundle"
                      : "Provision new volumes"}
                </p>
              </div>
              <div className="cluster">
                <p className="cluster-title">Mail & admin</p>
                <p><strong>Admin:</strong> {String(spec.admin_email || "—")}</p>
                <p><strong>SMTP sender:</strong> {String(spec.smtp_from || "—")}</p>
              </div>
              <div className="cluster">
                <p className="cluster-title">Container images</p>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {CORE_APPS.map((app) => (
                    <li key={app} style={{ fontFamily: "var(--font-mono)", fontSize: 13, marginBottom: 4 }}>
                      {APP_DEFAULTS[app].label}: {resolveAppImagePreview(app, getAppImageCfg(spec, app))}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            {restoreBundle?.present && reattachPolicy.mode === "provision_new" && (
              <label style={{ display: "flex", gap: 10, alignItems: "flex-start", marginTop: 16 }}>
                <input
                  type="checkbox"
                  checked={restoreFromBundle.enabled}
                  onChange={(e) => setRestoreFromBundleEnabled(e.target.checked)}
                  style={{ marginTop: 4 }}
                />
                <span>
                  <strong>Restore database from local bundle</strong>
                  <span className="muted" style={{ display: "block", fontSize: 13, marginTop: 4 }}>
                    Applies {restoreBundle.manifest?.db_backup_filename || "saved dump"} (
                    {restoreBundle.manifest?.db_backup_date} {restoreBundle.manifest?.db_backup_time} UTC) after HCCE
                    deploys on fresh PostgreSQL storage. Reticulum uploads are not included yet.
                  </span>
                </span>
              </label>
            )}
            {reattachBlocked && (
              <p className="bad-text">
                Cannot recreate with saved volumes — secrets.json is missing. Use the same instance data directory or
                restore from your secrets ZIP.
              </p>
            )}
            {restoreBundleBlocked && (
              <p className="bad-text">
                Restore from bundle is enabled but restore-bundle/ is missing — save a bundle before destroy or disable
                this option.
              </p>
            )}
            <p className="muted">
              {isRecreateFlow
                ? restoreFromBundle.enabled
                  ? "Recreate cluster will rebuild servers, create new volumes, deploy HCCE, then restore PostgreSQL from the local dump (~30–60+ minutes)."
                  : "Recreate cluster will rebuild servers and rebind saved volumes (~30–60+ minutes)."
                : "Create cluster will call Hetzner and run ~30–60+ minutes unattended."}
            </p>
            <button
              type="button"
              className="btn btn-primary"
              disabled={startBlocked}
              onClick={() => setConfirmOpen(true)}
            >
              {isRecreateFlow ? "Recreate cluster" : "Create cluster"}
            </button>
          </>
        )}

        <div className="wizard-footer">
          <button type="button" className="btn btn-ghost" disabled={step === 0} onClick={() => setStep((s) => s - 1)}>
            Back
          </button>
          <span className="muted">{saved ? "Saved" : ""}</span>
          <div style={{ display: "flex", gap: 8 }}>
            {step === 5 && (
              <button type="button" className="btn btn-outline" onClick={() => void goNext()}>
                Skip
              </button>
            )}
            {step < 6 && (
              <button type="button" className="btn btn-primary" disabled={!canContinue} onClick={() => void goNext()}>
                Continue
              </button>
            )}
          </div>
        </div>
      </section>

      <aside className="context-panel-wrap">
        <div className="context-panel">
        <p className="context-panel-header">Context</p>
        {step === 0 && <p>Token is stored only under <code>/data</code> on this machine. Click <strong>Validate token</strong> — you cannot continue until Hetzner accepts it.</p>}
        {step === 1 && spec.hub_domain ? (
          <>
            <p className="muted" style={{ marginTop: 0 }}>TLS terminated at HAProxy (cert-manager).</p>
            <ul style={{ paddingLeft: 18, margin: "12px 0 0" }}>
              <li>https://{String(spec.hub_domain)}</li>
              <li>assets.{String(spec.hub_domain)}</li>
              <li>stream.{String(spec.hub_domain)}</li>
              <li>cors.{String(spec.hub_domain)}</li>
            </ul>
          </>
        ) : step === 1 ? (
          <p style={{ margin: 0 }}>TLS terminated at HAProxy (cert-manager).</p>
        ) : null}
        {step === 2 && <ClusterTopology compact />}
        {step === 3 && <p style={{ margin: 0 }}>Required for invites and password reset mail.</p>}
        {step === 4 && (
          <p>
            Reticulum, Hubs, and Spoke are the core app containers. Other HCCE images (Postgres, HAProxy sidecars,
            etc.) stay on the template defaults.
          </p>
        )}
        {step === 5 && <p>Skip if unused — features stay limited.</p>}
        {step === 6 && (
          <p style={{ margin: "16px 0 0" }}>
            Phase B (workloads) starts automatically after the cluster is ready — no second click. CCM, CSI, and
            metrics-server are installed but not shown on the diagram.
          </p>
        )}
        </div>
      </aside>

      {confirmOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div className="modal-card">
            <h3 id="confirm-title">{isRecreateFlow ? "Recreate cluster?" : "Start provisioning?"}</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              {String(spec.hub_domain)} · {String(spec.location)} · {resolvedTypes.master}/{resolvedTypes.web}/{resolvedTypes.webrtc}
            </p>
            {isRecreateFlow ? (
              <p className="muted" style={{ marginBottom: 0 }}>
                {reattachPolicy.mode === "reattach_saved"
                  ? "Rebuilds Hetzner servers and Kubernetes, then rebinds saved block volumes (PostgreSQL / Reticulum data). Existing CSI volumes and any leftover load balancer continue billing until deleted in Console."
                  : restoreFromBundle.enabled
                    ? "Rebuilds Hetzner servers with new block volumes, deploys HCCE, then restores PostgreSQL from the local restore bundle."
                    : "Rebuilds Hetzner servers and Kubernetes with new block volumes."}
              </p>
            ) : (
              <p className="muted" style={{ marginBottom: 0 }}>
                This creates Hetzner Cloud servers, a load balancer, and volumes on your account. Those resources bill
                continuously until you delete them in the Hetzner Cloud Console.
              </p>
            )}
            <div className="modal-actions">
              <button type="button" className="btn btn-outline" onClick={() => setConfirmOpen(false)}>Cancel</button>
              <button type="button" className="btn btn-primary" onClick={() => { setConfirmOpen(false); void startCreate(); }}>
                {isRecreateFlow ? "Start recreate" : "Start provisioning"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
    </div>
  );
}
