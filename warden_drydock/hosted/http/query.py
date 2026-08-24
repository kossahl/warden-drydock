from __future__ import annotations

import re
import urllib.parse


_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def parse_flat_query(
    raw: str,
    *,
    singleton: frozenset[str],
    repeated: frozenset[str] = frozenset(),
    required: frozenset[str] = frozenset(),
) -> dict[str, str | tuple[str, ...]]:
    allowed = singleton | repeated
    values: dict[str, list[str]] = {}
    if raw:
        for field in raw.split("&"):
            if not field or "=" not in field:
                raise ValueError("invalid_query_binding")
            encoded_name, encoded_value = field.split("=", 1)
            if _BAD_PERCENT.search(encoded_name) or _BAD_PERCENT.search(encoded_value):
                raise ValueError("invalid_query_binding")
            try:
                name = urllib.parse.unquote_plus(encoded_name, encoding="utf-8", errors="strict")
                value = urllib.parse.unquote_plus(encoded_value, encoding="utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError("invalid_query_binding") from exc
            if name not in allowed or not name:
                raise ValueError("invalid_query_binding")
            if name in singleton and name in values:
                raise ValueError("invalid_query_binding")
            if name in repeated and "," in value:
                raise ValueError("invalid_query_binding")
            values.setdefault(name, []).append(value)
    if any(name not in values or values[name] == [""] for name in required):
        raise ValueError("invalid_query_binding")
    result: dict[str, str | tuple[str, ...]] = {}
    for name, items in values.items():
        if name in repeated:
            if any(item == "" for item in items):
                raise ValueError("invalid_query_binding")
            result[name] = tuple(sorted(set(items)))
        else:
            result[name] = items[0]
    return result


def require_int(value: object, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise ValueError("invalid_query_binding")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("invalid_query_binding")
    return parsed


def serialize_flat_query(
    values: dict[str, object], *, repeated: frozenset[str] = frozenset()
) -> str:
    pairs: list[tuple[str, str]] = []
    for name in sorted(values):
        value = values[name]
        if value is None:
            continue
        if name in repeated:
            if not isinstance(value, (tuple, list)):
                raise ValueError("invalid_query_binding")
            for item in sorted(set(value)):
                if not isinstance(item, str) or not item or "," in item:
                    raise ValueError("invalid_query_binding")
                pairs.append((name, item))
        else:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("invalid_query_binding")
            pairs.append((name, str(value)))
    return urllib.parse.urlencode(pairs, doseq=True, quote_via=urllib.parse.quote)
