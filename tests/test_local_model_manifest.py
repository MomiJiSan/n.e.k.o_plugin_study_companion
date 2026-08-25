from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_model_manifest_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE

manifest = importlib.import_module(f"{PACKAGE_NAME}.local_model_manifest")
protocol = importlib.import_module(f"{PACKAGE_NAME}.local_runtime_protocol")
LocalModelAssetError = manifest.LocalModelAssetError
LocalModelCatalog = manifest.LocalModelCatalog


def _payload(*, name: str = "weights/model.bin") -> dict[str, object]:
    content = b"test-model"
    return {
        "catalog_version": 1,
        "allowed_hosts": ["assets.example.test"],
        "packages": [
            {
                "id": "math_recognizer_demo",
                "version": "1.0.0",
                "role": "recognizer",
                "runtime_protocol": protocol.PROTOCOL_VERSION,
                "license": {
                    "name": "Apache-2.0",
                    "url": "https://assets.example.test/licenses/apache-2.0",
                    "requires_acceptance": False,
                },
                "files": [
                    {
                        "name": name,
                        "url": "https://assets.example.test/models/model.bin",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    }
                ],
            }
        ],
    }


def test_catalog_accepts_plan_schema_and_empty_production_catalog() -> None:
    catalog = LocalModelCatalog.from_payload(_payload())
    package = catalog.package("math_recognizer_demo", "1.0.0")
    assert package.role == "recognizer"
    assert package.runtime_protocol == protocol.PROTOCOL_VERSION
    assert package.license.requires_acceptance is False
    assert package.files[0].name == "weights/model.bin"

    production = LocalModelCatalog.load(ROOT / "local_models" / "catalog.v1.json")
    assert production.packages == ()


def test_package_acceptance_can_be_explicit_and_must_be_boolean() -> None:
    payload = _payload()
    package = payload["packages"][0]
    assert isinstance(package, dict)
    package["requires_acceptance"] = True
    assert LocalModelCatalog.from_payload(payload).packages[0].requires_acceptance is True
    package["requires_acceptance"] = "yes"
    with pytest.raises(LocalModelAssetError) as raised:
        LocalModelCatalog.from_payload(payload)
    assert raised.value.code == "local_model_catalog_invalid"


def test_package_cannot_disable_license_acceptance_requirement() -> None:
    payload = _payload()
    package = payload["packages"][0]
    assert isinstance(package, dict)
    license_payload = package["license"]
    assert isinstance(license_payload, dict)
    license_payload["requires_acceptance"] = True
    package["requires_acceptance"] = False

    parsed = LocalModelCatalog.from_payload(payload).packages[0]

    assert parsed.requires_acceptance is True


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/drive", "folder\\file", "CON", "COM1.bin", "x/.."])
def test_catalog_rejects_path_traversal_and_windows_device_names(name: str) -> None:
    with pytest.raises(LocalModelAssetError) as raised:
        LocalModelCatalog.from_payload(_payload(name=name))
    assert raised.value.code == "local_model_path_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "formula_ocr"),
        ("runtime_protocol", 999),
        ("version", "v1"),
    ],
)
def test_catalog_rejects_unsupported_role_protocol_and_version(field: str, value: object) -> None:
    payload = _payload()
    package = payload["packages"][0]
    assert isinstance(package, dict)
    package[field] = value
    with pytest.raises(LocalModelAssetError) as raised:
        LocalModelCatalog.from_payload(payload)
    assert raised.value.code in {"local_model_catalog_invalid", "local_model_version_unsupported"}


@pytest.mark.parametrize(
    "url",
    [
        "http://assets.example.test/models/model.bin",
        "https://unknown.example.test/models/model.bin",
        "https://assets.example.test:444/models/model.bin",
        "https://user@assets.example.test/models/model.bin",
        "https://assets.example.test/models/model.bin?q=1",
    ],
)
def test_catalog_requires_https_allowlisted_clean_urls(url: str) -> None:
    payload = _payload()
    package = payload["packages"][0]
    assert isinstance(package, dict)
    files = package["files"]
    assert isinstance(files, list) and isinstance(files[0], dict)
    files[0]["url"] = url
    with pytest.raises(LocalModelAssetError) as raised:
        LocalModelCatalog.from_payload(payload)
    assert raised.value.code == "local_model_catalog_invalid"


def test_error_payload_is_sanitized() -> None:
    error = LocalModelAssetError("local_model_path_invalid", "C:/secret/token")
    assert error.to_payload() == {"error": {"code": "local_model_path_invalid"}}
    assert "secret" not in json.dumps(error.to_payload())
