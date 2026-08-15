from __future__ import annotations

from typing import Any, Literal

import httpx

from services.hetzner import is_private_ip

CheckStatus = Literal["ok", "warn", "fail", "skip", "unknown"]

HCCE_CRITICAL_DEPLOYMENTS = (
    "reticulum",
    "pgsql",
    "pgbouncer",
    "hubs",
    "spoke",
    "dialog",
    "coturn",
)

EXPECTED_WORKLOAD_LABELS = {
    "hcce-master-db": "database",
    "hcce-web-worker": "web",
    "hcce-webrtc-worker": "webrtc",
}

SVCLB_MASTER_LABELS = {
    "svccontroller.k3s.cattle.io/enablelb": "true",
    "svccontroller.k3s.cattle.io/lbpool": "master-only",
}


def _check(
    check_id: str,
    name: str,
    group: str,
    status: CheckStatus,
    detail: str,
    *,
    hint: str = "",
) -> dict[str, str]:
    return {
        "id": check_id,
        "name": name,
        "group": group,
        "status": status,
        "detail": detail,
        "hint": hint,
    }


def _worst(*statuses: CheckStatus) -> CheckStatus:
    order = {"fail": 0, "warn": 1, "unknown": 2, "skip": 3, "ok": 4}
    return min(statuses, key=lambda s: order.get(s, 2))


def _overall(checks: list[dict[str, str]]) -> CheckStatus:
    statuses = [c["status"] for c in checks if c["status"] != "skip"]
    if not statuses:
        return "unknown"
    return _worst(*statuses)  # type: ignore[arg-type]


def probe_hub_https(hub_domain: str) -> tuple[CheckStatus, str]:
    if not hub_domain:
        return "skip", "No hub domain configured"
    url = f"https://{hub_domain}/"
    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, verify=True) as client:
            r = client.get(url)
        if r.status_code >= 500:
            return "warn", f"HTTP {r.status_code} from {url}"
        return "ok", f"HTTP {r.status_code} from {url}"
    except httpx.ConnectError:
        return "fail", f"Cannot connect to {url}"
    except httpx.TimeoutException:
        return "warn", f"Timed out reaching {url}"
    except Exception as e:
        msg = str(e).strip()
        if "certificate" in msg.lower() or "ssl" in msg.lower() or "tls" in msg.lower():
            return "warn", f"TLS not ready: {msg[:100]}"
        return "warn", msg[:120] or "Probe failed"


def evaluate_health(
    *,
    snapshot: dict[str, Any],
    hub_domain: str = "",
    lb_ip: str = "",
    dns_pending: bool = False,
    pipeline_state: str = "pending",
    probe_public: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    if snapshot.get("error"):
        checks.append(
            _check(
                "kubeconfig",
                "Cluster access",
                "infrastructure",
                "fail" if pipeline_state == "succeeded" else "unknown",
                snapshot["error"],
                hint="Wait for kubeconfig or re-run provisioning.",
            )
        )
        return {"overall": _overall(checks), "checks": checks}

    nodes = snapshot.get("nodes") or []
    pods = snapshot.get("pods") or []
    deployments = snapshot.get("deployments") or []
    certificates = snapshot.get("certificates") or []
    loadbalancers = snapshot.get("loadbalancers") or []

    # --- Infrastructure ---
    if not nodes:
        checks.append(
            _check("nodes_ready", "Nodes", "infrastructure", "unknown", "No nodes reported yet")
        )
    else:
        ready = [n for n in nodes if n.get("status") == "True"]
        if len(ready) == len(nodes) and len(nodes) >= 3:
            checks.append(
                _check("nodes_ready", "Nodes", "infrastructure", "ok", f"{len(ready)}/{len(nodes)} Ready")
            )
        else:
            checks.append(
                _check(
                    "nodes_ready",
                    "Nodes",
                    "infrastructure",
                    "fail" if len(ready) < len(nodes) else "warn",
                    f"{len(ready)}/{len(nodes)} Ready",
                    hint="Check cloud-init and k3s on workers.",
                )
            )

        label_issues: list[str] = []
        for node_name, expected in EXPECTED_WORKLOAD_LABELS.items():
            node = next((n for n in nodes if n.get("name") == node_name), None)
            if not node:
                label_issues.append(f"{node_name} missing")
                continue
            actual = (node.get("labels") or {}).get("workload-type", "")
            if actual != expected:
                label_issues.append(f"{node_name}: want {expected}, got {actual or 'unset'}")
        if not label_issues:
            checks.append(
                _check("node_workload_labels", "Workload labels", "infrastructure", "ok", "All 3 nodes labeled")
            )
        else:
            checks.append(
                _check(
                    "node_workload_labels",
                    "Workload labels",
                    "infrastructure",
                    "fail",
                    "; ".join(label_issues),
                    hint="Pipeline should apply workload-type labels before HAProxy.",
                )
            )

        master = next((n for n in nodes if n.get("name") == "hcce-master-db"), None)
        if master:
            labels = master.get("labels") or {}
            missing = [k for k, v in SVCLB_MASTER_LABELS.items() if labels.get(k) != v]
            if not missing:
                checks.append(
                    _check("svclb_labels", "ServiceLB pool", "infrastructure", "ok", "Master pinned for ingress LB")
                )
            else:
                checks.append(
                    _check(
                        "svclb_labels",
                        "ServiceLB pool",
                        "infrastructure",
                        "fail",
                        f"Master missing labels: {', '.join(missing)}",
                        hint="Label master with enablelb + lbpool=master-only so dialog/coturn hostPorts stay free.",
                    )
                )

        svclb_bad = [
            p
            for p in pods
            if p.get("namespace") == "kube-system"
            and "svclb-haproxy-ingress-lb" in p.get("name", "")
            and p.get("node") in ("hcce-web-worker", "hcce-webrtc-worker")
        ]
        if not any(n.get("name") == "hcce-master-db" for n in nodes):
            checks.append(
                _check("svclb_placement", "ServiceLB pods", "infrastructure", "unknown", "Master node not found")
            )
        elif svclb_bad:
            checks.append(
                _check(
                    "svclb_placement",
                    "ServiceLB pods",
                    "infrastructure",
                    "fail",
                    f"{len(svclb_bad)} svclb pod(s) on web/webrtc nodes",
                    hint="Delete stray svclb pods after fixing master labels.",
                )
            )
        else:
            checks.append(
                _check("svclb_placement", "ServiceLB pods", "infrastructure", "ok", "No svclb on WebRTC/web workers")
            )

    # --- Platform ---
    lb = loadbalancers[0] if loadbalancers else {}
    public_ip = lb.get("external_ip") or lb_ip or ""
    if public_ip and not is_private_ip(public_ip):
        checks.append(
            _check("load_balancer", "Load balancer IP", "platform", "ok", public_ip)
        )
    elif dns_pending or not public_ip:
        checks.append(
            _check(
                "load_balancer",
                "Load balancer IP",
                "platform",
                "warn",
                "Public IP not resolved yet",
                hint="Refresh progress page — Hetzner API lookup may still be running.",
            )
        )
    else:
        checks.append(
            _check(
                "load_balancer",
                "Load balancer IP",
                "platform",
                "fail",
                f"Private or invalid IP: {public_ip}",
            )
        )

    haproxy = next(
        (d for d in deployments if d.get("namespace") == "haproxy" and d.get("name") == "haproxy-ingress"),
        None,
    )
    if haproxy:
        ready, status = _deployment_ready(haproxy)
        checks.append(
            _check(
                "haproxy",
                "HAProxy ingress",
                "platform",
                status,
                ready,
                hint="Check haproxy namespace pods and master node placement.",
            )
        )
    elif pipeline_state == "succeeded":
        checks.append(
            _check("haproxy", "HAProxy ingress", "platform", "warn", "Deployment not found")
        )

    # --- HCCE ---
    hcce_deps = {d["name"]: d for d in deployments if d.get("namespace") == "hcce"}
    if not hcce_deps and pipeline_state not in ("succeeded", "running"):
        checks.append(
            _check("hcce_stack", "HCCE workloads", "hcce", "skip", "Cluster not provisioned yet")
        )
    else:
        for name in HCCE_CRITICAL_DEPLOYMENTS:
            dep = hcce_deps.get(name)
            if not dep:
                st: CheckStatus = "warn" if pipeline_state == "running" else "fail"
                checks.append(_check(f"hcce_{name}", name, "hcce", st, "Deployment missing"))
                continue
            ready, st = _deployment_ready(dep)
            bad_pods = _bad_pods_for_workload(pods, "hcce", name)
            detail = ready
            if bad_pods:
                detail = f"{ready}; {bad_pods}"
            hint = ""
            if st == "fail" and name in ("dialog", "coturn"):
                hint = "Often caused by svclb port conflicts on the WebRTC node."
            elif st == "fail" and name == "reticulum":
                hint = "Check configs secret (PERMS_KEY format) and reticulum logs."
            checks.append(_check(f"hcce_{name}", name, "hcce", st, detail, hint=hint))

    # --- TLS ---
    hcce_certs = [c for c in certificates if c.get("namespace") == "hcce"]
    if not hcce_certs:
        if pipeline_state == "succeeded":
            checks.append(_check("certificates", "TLS certificates", "tls", "warn", "No certificates found"))
        else:
            checks.append(_check("certificates", "TLS certificates", "tls", "skip", "Not deployed yet"))
    else:
        ready_certs = [c for c in hcce_certs if c.get("ready")]
        if len(ready_certs) == len(hcce_certs):
            checks.append(
                _check("certificates", "TLS certificates", "tls", "ok", f"{len(ready_certs)}/{len(hcce_certs)} Ready")
            )
        else:
            checks.append(
                _check(
                    "certificates",
                    "TLS certificates",
                    "tls",
                    "warn",
                    f"{len(ready_certs)}/{len(hcce_certs)} Ready",
                    hint="Ensure DNS A records point at the LB public IP. If hub or stream certs stay Not ready, check that ret/dialog ingresses do not use ssl-redirect (breaks HTTP-01 on /).",
                )
            )

    # --- Public reachability ---
    if probe_public and hub_domain and pipeline_state == "succeeded":
        st, detail = probe_hub_https(hub_domain)
        checks.append(
            _check(
                "hub_https",
                "Hub HTTPS",
                "public",
                st,
                detail,
                hint="Fix TLS certs and reticulum before expecting a clean HTTPS response.",
            )
        )
    elif hub_domain:
        checks.append(
            _check("hub_https", "Hub HTTPS", "public", "skip", "Available after provisioning completes")
        )

    return {"overall": _overall(checks), "checks": checks}


def _deployment_ready(dep: dict[str, Any]) -> tuple[str, CheckStatus]:
    ready_str = dep.get("ready", "0/0")
    try:
        ready, total = ready_str.split("/")
        ready_n, total_n = int(ready), int(total)
    except (ValueError, AttributeError):
        return ready_str, "unknown"
    if total_n == 0:
        return ready_str, "warn"
    if ready_n >= total_n:
        return ready_str, "ok"
    if ready_n == 0:
        return ready_str, "fail"
    return ready_str, "warn"


def _bad_pods_for_workload(pods: list[dict[str, Any]], namespace: str, prefix: str) -> str:
    issues: list[str] = []
    for p in pods:
        if p.get("namespace") != namespace:
            continue
        if not p.get("name", "").startswith(prefix):
            continue
        phase = p.get("status", "")
        reason = p.get("reason", "")
        if phase in ("Failed", "Unknown"):
            issues.append(f"{p['name']}: {phase}")
        elif phase == "Pending" and reason:
            issues.append(f"{p['name']}: {reason}")
        elif reason in ("CrashLoopBackOff", "Error", "ImagePullBackOff", "CreateContainerConfigError"):
            issues.append(f"{p['name']}: {reason}")
    return "; ".join(issues[:3])
