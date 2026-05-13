"""Normalize MOBILE_* settings into a coherent latest release for the update API."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class ResolvedMobileLatest:
    latest_version: str | None
    latest_build: int
    latest_version_plus: str | None


def resolve_mobile_latest(settings: Settings) -> ResolvedMobileLatest | None:
    """
    Merge MOBILE_LATEST_VERSION and MOBILE_LATEST_BUILD_NUMBER.

    MOBILE_LATEST_VERSION may be \"1.0.8\", \"1.0.8+2\", or empty.
    If a +build suffix is present, it overrides MOBILE_LATEST_BUILD_NUMBER for that field.
    """
    raw = (settings.MOBILE_LATEST_VERSION or "").strip()
    env_build = int(settings.MOBILE_LATEST_BUILD_NUMBER or 0)

    base = raw
    build = env_build
    if "+" in raw:
        base, _, rest = raw.partition("+")
        base = base.strip()
        tail = rest.strip()
        if tail.isdigit():
            build = int(tail)
    else:
        base = raw.strip()

    has_version = bool(base)
    if not has_version and build <= 0:
        return None

    latest_version = base if has_version else None
    latest_build = max(0, build)

    if latest_version is not None:
        latest_version_plus = (
            f"{latest_version}+{latest_build}" if latest_build > 0 else latest_version
        )
    else:
        latest_version_plus = None

    return ResolvedMobileLatest(
        latest_version=latest_version,
        latest_build=latest_build,
        latest_version_plus=latest_version_plus,
    )
