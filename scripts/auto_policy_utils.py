from __future__ import annotations


def normalize_upper_list(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


def normalize_clock_hhmm(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        parts = text.split(":", 1)
    elif len(text) == 4 and text.isdigit():
        parts = [text[:2], text[2:]]
    else:
        return ""
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def normalize_blackout_window(value: object) -> str:
    text = str(value or "").strip()
    if not text or "-" not in text:
        return ""
    start_raw, end_raw = text.split("-", 1)
    start_norm = normalize_clock_hhmm(start_raw)
    end_norm = normalize_clock_hhmm(end_raw)
    if not start_norm or not end_norm or start_norm > end_norm:
        return ""
    return f"{start_norm}-{end_norm}"


def normalize_blackout_windows(values: object) -> list[str]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        normalized = normalize_blackout_window(value)
        if normalized:
            out.append(normalized)
    return sorted(set(out))


def merge_blackout_windows(left: object, right: object) -> list[str]:
    return sorted(set(normalize_blackout_windows(left)) | set(normalize_blackout_windows(right)))


def normalize_group_blackout_slice(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text or "/" not in text:
        return ""
    portfolio_name, contour_name = text.split("/", 1)
    portfolio_name = portfolio_name.strip().upper()
    contour_name = contour_name.strip().upper()
    if not portfolio_name or contour_name not in {"STRICT", "AGGRESSIVE"}:
        return ""
    return f"{portfolio_name}/{contour_name}"


def normalize_group_blackout_windows(values: object) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, windows in values.items():
        group_key = normalize_group_blackout_slice(key)
        normalized_windows = normalize_blackout_windows(windows)
        if group_key and normalized_windows:
            out[group_key] = normalized_windows
    return {key: out[key] for key in sorted(out)}


def merge_group_blackout_windows(left: object, right: object) -> dict[str, list[str]]:
    left_norm = normalize_group_blackout_windows(left)
    right_norm = normalize_group_blackout_windows(right)
    out: dict[str, list[str]] = {}
    for key in sorted(set(left_norm) | set(right_norm)):
        merged = merge_blackout_windows(left_norm.get(key), right_norm.get(key))
        if merged:
            out[key] = merged
    return out


def count_group_blackout_rules(values: object) -> int:
    return sum(len(windows) for windows in normalize_group_blackout_windows(values).values())


def format_group_blackout_windows(values: object, empty: str = "none") -> str:
    normalized = normalize_group_blackout_windows(values)
    if not normalized:
        return empty
    return "; ".join(f"{key}={', '.join(windows)}" for key, windows in normalized.items())


def normalize_shadow_model_name(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_entry_shadow_gate_group_models(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, model in values.items():
        group_key = normalize_group_blackout_slice(key)
        model_name = normalize_shadow_model_name(model)
        if group_key and model_name:
            out[group_key] = model_name
    return {key: out[key] for key in sorted(out)}


def count_entry_shadow_gate_rules(values: object) -> int:
    return len(normalize_entry_shadow_gate_group_models(values))


def format_entry_shadow_gate_group_models(values: object, empty: str = "none") -> str:
    normalized = normalize_entry_shadow_gate_group_models(values)
    if not normalized:
        return empty
    return "; ".join(f"{key}={model}" for key, model in normalized.items())


def policy_group_blackout_windows(values: object) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        return {}
    if "entry_blackout_group_windows" in values or "group_blackout_windows" in values:
        return normalize_group_blackout_windows(values.get("entry_blackout_group_windows") or values.get("group_blackout_windows"))
    return normalize_group_blackout_windows(values)
