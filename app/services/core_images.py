from __future__ import annotations

import re
from typing import Literal

CoreAppName = Literal["reticulum", "hubs", "spoke"]
CoreAppImageMode = Literal["default", "pin", "custom"]

CORE_APP_NAMES: tuple[CoreAppName, ...] = ("reticulum", "hubs", "spoke")

# "default" mode deploys `image`, pinned by digest so every node and every pod
# restart runs the same build. Updates are deliberate: bump the digests here (and
# in APP_DEFAULTS in app/ui/src/pages/Wizard.tsx). Current pins are the
# `stable-latest` builds as of 2026-11-02 — reticulum build 855 (2026-03-12),
# hubs and spoke from 2026-10-29. `default_tag` is the pin-mode fallback only.
CORE_APP_DEFAULTS: dict[CoreAppName, dict[str, str]] = {
    "reticulum": {
        "repo": "hubsfoundation/reticulum",
        "default_tag": "stable-latest",
        "image": "hubsfoundation/reticulum@sha256:16fb2b23c0593d7639f4af8eb9eee3ddc3e5df1d5677a4854804a867e38aeb5e",
    },
    "hubs": {
        "repo": "hubsfoundation/hubs",
        "default_tag": "stable-latest",
        "image": "hubsfoundation/hubs@sha256:878fd63c687a5652266577f19c65de309f61dc9beb0ef0f71fbac73655c39cd9",
    },
    "spoke": {
        "repo": "hubsfoundation/spoke",
        "default_tag": "stable-latest",
        "image": "hubsfoundation/spoke@sha256:4c071b3184263a1fd53be0964bbd9fd1aa56fc21eba468c73f58e64d6b1f0ff2",
    },
}

# Support services with no wizard choice. Pinned by digest: these are the
# hubsfoundation `stable-latest` builds of 2026-10-29, which moved the Node.js
# services to current Node releases (Node2Shell, CVE-2026-111497; hubs-cloud
# 2.1.2). The mozillareality/* images are unmaintained (last builds 2022–2024).
# Bump the digests here when the Hubs Foundation ships a new build, as for
# CORE_APP_DEFAULTS.
SupportAppName = Literal["dialog", "nearspark", "photomnemonic"]
SUPPORT_APP_IMAGES: dict[SupportAppName, str] = {
    "dialog": "hubsfoundation/dialog@sha256:ae24bef57ed80a6ee723fd8c447eb28c149e2c9f27808efd1e7e302fc9539e0a",
    "nearspark": "hubsfoundation/nearspark@sha256:c2af7c30a78119a52872279cd0bdbb0fd2d98a6456e2d9fe8272afd6b8f846c3",
    "photomnemonic": "hubsfoundation/photomnemonic@sha256:ea598857aa1284acb96b0a6881f79cfb8cee62542a3c8e246beda94859d4715d",
}

# Reticulum replaced Bamboo with Swoosh for sending email on 2026-02-27
# (Hubs-Foundation/reticulum commit e7c3a0f). Swoosh's SMTP adapter requires the
# config key `relay`; Bamboo's required `server`. templates/hcce/hcce.yaml.j2
# ships `relay`, so a Reticulum image built before that date cannot send mail —
# it raises `(ArgumentError) expected [:relay] to be set` when a magic-link login
# is requested, while the rest of the deployment looks healthy.
RETICULUM_SWOOSH_DATE = "2026-02-27"
# hubsfoundation/reticulum tags are build numbers, optionally channel-prefixed
# ("855", "stable-855", "dev-858"), plus rolling "*-latest" and git SHAs — no
# dates. Build 851 was pushed 2026-01-20 (before the migration) and 852 on
# 2026-03-08 (after), so 852 is the first build that can contain Swoosh.
RETICULUM_MIN_BUILD = 852
RETICULUM_MIN_VERSION_NOTE = (
    f"Reticulum builds below {RETICULUM_MIN_BUILD} use Bamboo for email (the switch to Swoosh "
    f"landed {RETICULUM_SWOOSH_DATE}). This installer ships Swoosh's `relay` config key, so "
    f"older images cannot send login emails. Use stable-latest, or build {RETICULUM_MIN_BUILD} "
    "or higher."
)

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# "855", "stable-855", "beta-855", "dev-858" — the build number, if the tag is one.
_TAG_BUILD_RE = re.compile(r"^(?:stable-|beta-|dev-)?(\d{1,6})$")
# Public image refs only — no spaces; repo path + optional :tag or @digest
IMAGE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*)?(:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|@sha256:[a-f0-9]{64})?$",
    re.IGNORECASE,
)


def normalize_core_app_images(raw: dict | None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name in CORE_APP_NAMES:
        entry = (raw or {}).get(name) or {}
        mode = str(entry.get("mode") or "default").strip().lower()
        if mode not in ("default", "pin", "custom"):
            mode = "default"
        out[name] = {
            "mode": mode,
            "tag": str(entry.get("tag") or "").strip(),
            "image": str(entry.get("image") or "").strip(),
        }
    return out


def resolve_core_app_image(app: CoreAppName, settings: dict[str, dict[str, str]] | None) -> str:
    cfg = normalize_core_app_images(settings).get(app, {})
    meta = CORE_APP_DEFAULTS[app]
    mode = cfg.get("mode", "default")
    if mode == "custom":
        return cfg.get("image") or meta["image"]
    if mode == "pin":
        tag = cfg.get("tag") or meta["default_tag"]
        return f"{meta['repo']}:{tag}"
    return meta["image"]


def resolve_all_core_app_images(settings: dict[str, dict[str, str]] | None) -> dict[str, str]:
    return {app: resolve_core_app_image(app, settings) for app in CORE_APP_NAMES}


def image_pull_policy(ref: str) -> str:
    """IfNotPresent for a digest ref, Always for a tag.

    A rolling tag such as stable-latest with IfNotPresent keeps whatever build a
    node cached first, so security fixes never arrive. A digest names exactly one
    build, so a cached copy is always the right one.
    """
    return "IfNotPresent" if "@sha256:" in (ref or "") else "Always"


def _tag_predates_swoosh(ref: str) -> int | None:
    """Return the build number if the tag is a build we know predates Swoosh.

    Only decidable for numeric build tags. Rolling tags ("stable-latest") and git
    SHAs carry no ordering, so they cannot be checked here — the wizard shows
    RETICULUM_MIN_VERSION_NOTE for any non-default choice instead.
    """
    ref = (ref or "").strip()
    if "/" in ref or ":" in ref:  # custom image reference — take the tag part
        ref = ref.rsplit(":", 1)[-1] if ":" in ref else ""
    m = _TAG_BUILD_RE.match(ref)
    if not m:
        return None
    build = int(m.group(1))
    return build if build < RETICULUM_MIN_BUILD else None


def validate_core_app_images(settings: dict[str, dict[str, str]] | None) -> list[str]:
    errors: list[str] = []
    for app in CORE_APP_NAMES:
        cfg = normalize_core_app_images(settings)[app]
        mode = cfg["mode"]
        label = app.capitalize()
        if mode == "pin":
            tag = cfg["tag"]
            if not tag:
                errors.append(f"{label}: enter a tag to pin (e.g. stable-latest).")
            elif not TAG_RE.match(tag):
                errors.append(f"{label}: invalid tag format.")
        elif mode == "custom":
            image = cfg["image"]
            if not image:
                errors.append(f"{label}: enter a public container image reference.")
            elif " " in image or not IMAGE_RE.match(image):
                errors.append(f"{label}: invalid image reference (public images only; no spaces).")
        if app == "reticulum" and mode in ("pin", "custom"):
            ref = cfg["tag"] if mode == "pin" else cfg["image"]
            old = _tag_predates_swoosh(ref)
            if old is not None:
                errors.append(f"{label}: build {old} is too old. {RETICULUM_MIN_VERSION_NOTE}")
    return errors
