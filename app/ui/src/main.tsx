import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./styles/global.css";
import { Shell, LandingPage } from "./pages/Landing";
import { WizardPage } from "./pages/Wizard";
import { ProgressPage } from "./pages/Progress";
import { DocsPage } from "./pages/Docs";
import { api } from "./api";

function NewInstance() {
  const nav = useNavigate();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    api<{ id: string }>("/instances", { method: "POST" }).then((s) => {
      nav(`/instances/${s.id}/setup`, { replace: true });
      setReady(true);
    });
  }, [nav]);
  return ready ? null : <p className="muted">Creating instance…</p>;
}

function WizardRoute() {
  const { id } = useParams();
  if (!id) return <Navigate to="/" />;
  return <WizardPage instanceId={id} />;
}

function ProgressRoute() {
  const { id } = useParams();
  if (!id) return <Navigate to="/" />;
  return <ProgressPage instanceId={id} />;
}

function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/docs" element={<DocsPage />} />
        <Route path="/manage" element={<Navigate to="/" replace />} />
        <Route path="/instances/new" element={<NewInstance />} />
        <Route path="/instances/:id/setup" element={<WizardRoute />} />
        <Route path="/instances/:id" element={<ProgressRoute />} />
      </Routes>
    </Shell>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>
);
