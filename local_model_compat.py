"""Stable, side-effect-free compatibility payloads for paused local models."""

LOCAL_MODEL_UNAVAILABLE_CODE = "local_model_store_unavailable"


def directory_mode(config: object) -> str:
    """Preserve the directory setting without reading or creating its path."""

    value = str(getattr(config, "local_models_directory", "") or "").strip()
    return "custom" if value else "default"


def catalog_payload(config: object) -> dict[str, object]:
    """Return a fixed catalog while the local-model product is paused."""

    return {
        "available": False,
        "directory_mode": directory_mode(config),
        "packages": [],
        "error_code": LOCAL_MODEL_UNAVAILABLE_CODE,
    }


def status_payload(config: object) -> dict[str, object]:
    """Return a fixed status without touching disk, network, or a runtime."""

    return {
        **catalog_payload(config),
        "state": "unavailable",
        "installed": [],
        "downloads": [],
        "disk": {},
        "last": None,
    }


__all__ = [
    "LOCAL_MODEL_UNAVAILABLE_CODE",
    "catalog_payload",
    "directory_mode",
    "status_payload",
]
