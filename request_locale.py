from __future__ import annotations

SUPPORTED_REQUEST_LOCALES = frozenset(
    {"en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"}
)

_REQUEST_LOCALE_ALIASES = {
    "en-us": "en",
    "en-gb": "en",
    "es-es": "es",
    "ja-jp": "ja",
    "ko-kr": "ko",
    "pt-br": "pt",
    "pt-pt": "pt",
    "ru-ru": "ru",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh-hant": "zh-TW",
}


def normalize_request_locale(value: object, *, fallback: object = "zh-CN") -> str:
    """Return a supported learner-facing locale without trusting prompt input."""

    def normalized(candidate: object) -> str:
        raw = str(candidate or "").strip().replace("_", "-")
        return _REQUEST_LOCALE_ALIASES.get(raw.lower(), raw)

    requested = normalized(value)
    if requested in SUPPORTED_REQUEST_LOCALES:
        return requested
    default = normalized(fallback)
    return default if default in SUPPORTED_REQUEST_LOCALES else "zh-CN"


__all__ = ["SUPPORTED_REQUEST_LOCALES", "normalize_request_locale"]
