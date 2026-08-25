"""Strict, data-only manifest types for optional local model packages.

The catalog is intentionally separate from downloading and inference.  This
module validates all untrusted catalog fields before another component is ever
allowed to create a filesystem path or make a network request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

try:
    from .local_runtime_protocol import PROTOCOL_VERSION
except ImportError:  # Direct imports in isolated tests.
    from local_runtime_protocol import PROTOCOL_VERSION  # type: ignore[no-redef]


CATALOG_VERSION: Final = 1
MAX_FILE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_BYTES: Final = 2 * 1024 * 1024 * 1024
_PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SPDX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_ROLES = frozenset({"recognizer", "reasoner"})
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class LocalModelAssetError(ValueError):
    """A public asset error which deliberately contains no local path or URL."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "local_model_asset_invalid")
        super().__init__(message or self.code)

    def to_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code}}


def _invalid(code: str) -> LocalModelAssetError:
    return LocalModelAssetError(code, "local model asset metadata is invalid")


def _validate_package_id(value: object) -> str:
    package_id = str(value or "")
    if not _PACKAGE_ID_RE.fullmatch(package_id):
        raise _invalid("local_model_catalog_invalid")
    return package_id


def _validate_version(value: object) -> str:
    version = str(value or "")
    if not _SEMVER_RE.fullmatch(version):
        raise _invalid("local_model_version_unsupported")
    return version


def validate_relative_path(value: object) -> str:
    """Accept only portable child paths, never a host filesystem path."""

    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise _invalid("local_model_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or not path.parts:
        raise _invalid("local_model_path_invalid")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if part in {"", ".", ".."} or part.endswith((".", " ")) or stem in _WINDOWS_DEVICE_NAMES:
            raise _invalid("local_model_path_invalid")
    return path.as_posix()


def _validate_https_url(value: object, *, allowed_hosts: frozenset[str]) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid("local_model_catalog_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _invalid("local_model_catalog_invalid") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise _invalid("local_model_catalog_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ModelLicense:
    name: str
    url: str
    requires_acceptance: bool
    spdx: str = ""

    @classmethod
    def from_payload(cls, payload: object, *, allowed_hosts: frozenset[str]) -> "ModelLicense":
        if not isinstance(payload, dict):
            raise _invalid("local_model_catalog_invalid")
        name = payload.get("name")
        accepted = payload.get("requires_acceptance")
        spdx = payload.get("spdx", "")
        if (
            not isinstance(name, str)
            or not name.strip()
            or isinstance(accepted, bool) is False
            or not isinstance(spdx, str)
            or (spdx and not _SPDX_RE.fullmatch(spdx))
        ):
            raise _invalid("local_model_catalog_invalid")
        return cls(
            name=name.strip(),
            url=_validate_https_url(payload.get("url"), allowed_hosts=allowed_hosts),
            requires_acceptance=accepted,
            spdx=spdx,
        )


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size_bytes: int

    @property
    def path(self) -> str:
        """Internal portable relative path; catalog input uses the `name` key."""

        return self.name

    @classmethod
    def from_payload(cls, payload: object, *, allowed_hosts: frozenset[str]) -> "ModelFile":
        if not isinstance(payload, dict):
            raise _invalid("local_model_catalog_invalid")
        path = validate_relative_path(payload.get("name"))
        sha256 = str(payload.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise _invalid("local_model_catalog_invalid")
        size = payload.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
            raise _invalid("local_model_catalog_invalid")
        return cls(
            name=path,
            url=_validate_https_url(payload.get("url"), allowed_hosts=allowed_hosts),
            sha256=sha256,
            size_bytes=size,
        )


@dataclass(frozen=True, slots=True)
class ModelPackage:
    package_id: str
    version: str
    role: str
    runtime_protocol: int
    requires_acceptance: bool
    license: ModelLicense
    files: tuple[ModelFile, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @classmethod
    def from_payload(cls, payload: object, *, allowed_hosts: frozenset[str]) -> "ModelPackage":
        if not isinstance(payload, dict):
            raise _invalid("local_model_catalog_invalid")
        role = payload.get("role")
        if not isinstance(role, str) or role not in _ROLES:
            raise _invalid("local_model_catalog_invalid")
        runtime_protocol = payload.get("runtime_protocol")
        if runtime_protocol != PROTOCOL_VERSION:
            raise _invalid("local_model_version_unsupported")
        license = ModelLicense.from_payload(payload.get("license"), allowed_hosts=allowed_hosts)
        package_acceptance = payload.get("requires_acceptance", False)
        if not isinstance(package_acceptance, bool):
            raise _invalid("local_model_catalog_invalid")
        # A package may strengthen the license requirement, but can never
        # override a license which itself requires explicit acceptance.
        requires_acceptance = license.requires_acceptance or package_acceptance
        files_payload = payload.get("files")
        if not isinstance(files_payload, list) or not files_payload:
            raise _invalid("local_model_catalog_invalid")
        files = tuple(ModelFile.from_payload(item, allowed_hosts=allowed_hosts) for item in files_payload)
        if len({item.path for item in files}) != len(files):
            raise _invalid("local_model_catalog_invalid")
        if sum(item.size_bytes for item in files) > MAX_PACKAGE_BYTES:
            raise _invalid("local_model_catalog_invalid")
        return cls(
            package_id=_validate_package_id(payload.get("id")),
            version=_validate_version(payload.get("version")),
            role=role,
            runtime_protocol=runtime_protocol,
            requires_acceptance=requires_acceptance,
            license=license,
            files=files,
        )


@dataclass(frozen=True, slots=True)
class LocalModelCatalog:
    version: int
    allowed_hosts: frozenset[str]
    packages: tuple[ModelPackage, ...]

    @classmethod
    def from_payload(cls, payload: object) -> "LocalModelCatalog":
        if not isinstance(payload, dict):
            raise _invalid("local_model_catalog_invalid")
        version = payload.get("catalog_version")
        if version != CATALOG_VERSION:
            raise _invalid("local_model_version_unsupported")
        hosts_payload = payload.get("allowed_hosts")
        if not isinstance(hosts_payload, list):
            raise _invalid("local_model_catalog_invalid")
        allowed_hosts = frozenset(str(host).lower().rstrip(".") for host in hosts_payload if isinstance(host, str))
        if len(allowed_hosts) != len(hosts_payload) or any(
            not host or "/" in host or ":" in host or " " in host for host in allowed_hosts
        ):
            raise _invalid("local_model_catalog_invalid")
        packages_payload = payload.get("packages")
        if not isinstance(packages_payload, list):
            raise _invalid("local_model_catalog_invalid")
        packages = tuple(ModelPackage.from_payload(item, allowed_hosts=allowed_hosts) for item in packages_payload)
        identities = {(item.package_id, item.version) for item in packages}
        if len(identities) != len(packages):
            raise _invalid("local_model_catalog_invalid")
        return cls(version=version, allowed_hosts=allowed_hosts, packages=packages)

    @classmethod
    def load(cls, path: str | Path) -> "LocalModelCatalog":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise _invalid("local_model_catalog_invalid") from exc
        return cls.from_payload(payload)

    def package(self, package_id: str, version: str) -> ModelPackage:
        package_id = _validate_package_id(package_id)
        version = _validate_version(version)
        for item in self.packages:
            if item.package_id == package_id and item.version == version:
                return item
        raise LocalModelAssetError("local_model_unknown", "local model package is unavailable")


__all__ = [
    "CATALOG_VERSION",
    "LocalModelAssetError",
    "LocalModelCatalog",
    "MAX_FILE_BYTES",
    "MAX_PACKAGE_BYTES",
    "ModelFile",
    "ModelLicense",
    "ModelPackage",
    "validate_relative_path",
]
