from __future__ import annotations

from auto_policy_utils import (
    count_group_blackout_rules,
    merge_group_blackout_windows,
    normalize_blackout_windows,
    normalize_upper_list,
    policy_group_blackout_windows,
)


NUMERIC_POLICY_KEYS = (
    "entry_no_trade_before",
    "entry_no_new_after",
    "entry_max_full_stop_rub",
    "pause_ticker_after_losses",
    "pause_family_after_losses",
    "pause_after_loss_minutes",
)


def normalize_notes(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def normalize_policy_view(view: dict | None) -> dict:
    view = view if isinstance(view, dict) else {}
    normalized: dict = {
        "observe_only_portfolios": normalize_upper_list(view.get("observe_only_portfolios")),
        "observe_only_group_families": normalize_upper_list(view.get("observe_only_group_families")),
        "allow_aggressive_group_families": normalize_upper_list(view.get("allow_aggressive_group_families")),
        "observe_only_tickers": normalize_upper_list(view.get("observe_only_tickers")),
        "observe_only_families": normalize_upper_list(view.get("observe_only_families")),
        "strict_only_tickers": normalize_upper_list(view.get("strict_only_tickers")),
        "strict_only_families": normalize_upper_list(view.get("strict_only_families")),
        "entry_blackout_windows": normalize_blackout_windows(view.get("entry_blackout_windows")),
        "entry_blackout_group_windows": policy_group_blackout_windows(view),
        "notes": normalize_notes(view.get("notes")),
    }
    for key in NUMERIC_POLICY_KEYS:
        normalized[key] = view.get(key)
    return normalized


def policy_functional_signature(view: dict | None) -> dict:
    normalized = normalize_policy_view(view)
    normalized.pop("notes", None)
    return normalized


def merge_policy_views(base_active: dict, overrides: dict) -> dict:
    merged = dict(base_active) if isinstance(base_active, dict) else {}
    base_active = base_active if isinstance(base_active, dict) else {}
    overrides = overrides if isinstance(overrides, dict) else {}
    merged["observe_only_portfolios"] = normalize_upper_list(base_active.get("observe_only_portfolios"))
    merged["observe_only_group_families"] = sorted(
        set(normalize_upper_list(base_active.get("observe_only_group_families")))
        | set(normalize_upper_list(overrides.get("observe_only_group_families")))
    )
    merged["allow_aggressive_group_families"] = normalize_upper_list(base_active.get("allow_aggressive_group_families"))
    merged["observe_only_tickers"] = sorted(
        set(normalize_upper_list(base_active.get("observe_only_tickers"))) | set(normalize_upper_list(overrides.get("observe_only_tickers")))
    )
    merged["observe_only_families"] = sorted(
        set(normalize_upper_list(base_active.get("observe_only_families"))) | set(normalize_upper_list(overrides.get("observe_only_families")))
    )
    merged["strict_only_tickers"] = normalize_upper_list(base_active.get("strict_only_tickers"))
    merged["strict_only_families"] = normalize_upper_list(base_active.get("strict_only_families"))
    merged["entry_blackout_windows"] = normalize_blackout_windows(base_active.get("entry_blackout_windows"))
    merged["entry_blackout_group_windows"] = merge_group_blackout_windows(
        policy_group_blackout_windows(base_active),
        policy_group_blackout_windows(overrides),
    )
    for key in NUMERIC_POLICY_KEYS:
        merged[key] = base_active.get(key)
    base_notes = [str(item) for item in (base_active.get("notes") or []) if str(item).strip()]
    override_notes = [str(item) for item in (overrides.get("notes") or []) if str(item).strip()]
    merged["notes"] = list(dict.fromkeys(base_notes + override_notes))[:12]
    return merged


def strip_watchdog_overrides(active: dict, overrides: dict) -> dict:
    base = dict(active) if isinstance(active, dict) else {}
    overrides = overrides if isinstance(overrides, dict) else {}
    base["observe_only_portfolios"] = normalize_upper_list(base.get("observe_only_portfolios"))
    group_values = normalize_upper_list(base.get("observe_only_group_families"))
    remove_group_values = set(normalize_upper_list(overrides.get("observe_only_group_families")))
    base["observe_only_group_families"] = [value for value in group_values if value not in remove_group_values]
    base["allow_aggressive_group_families"] = normalize_upper_list(base.get("allow_aggressive_group_families"))
    for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families"):
        values = normalize_upper_list(base.get(key))
        if key in {"observe_only_tickers", "observe_only_families"}:
            remove_values = set(normalize_upper_list(overrides.get(key)))
            values = [value for value in values if value not in remove_values]
        base[key] = values
    base["entry_blackout_group_windows"] = policy_group_blackout_windows(base)
    notes = [str(item) for item in (base.get("notes") or []) if str(item).strip()]
    remove_notes = {str(item) for item in (overrides.get("notes") or []) if str(item).strip()}
    base["notes"] = [note for note in notes if note not in remove_notes]
    return base


def summarize_active_policy(active: dict) -> dict[str, int]:
    active = normalize_policy_view(active)
    return {
        "active_rule_count": (
            len(active.get("observe_only_portfolios") or [])
            + len(active.get("observe_only_group_families") or [])
            + len(active.get("allow_aggressive_group_families") or [])
            + len(active.get("observe_only_tickers") or [])
            + len(active.get("observe_only_families") or [])
            + len(active.get("strict_only_tickers") or [])
            + len(active.get("strict_only_families") or [])
            + len(active.get("entry_blackout_windows") or [])
            + count_group_blackout_rules(policy_group_blackout_windows(active))
            + sum(1 for key in NUMERIC_POLICY_KEYS if active.get(key) not in (None, ""))
        ),
        "active_notes_count": len(active.get("notes") or []),
    }


def merge_watchdog_overrides(auto_policy: dict, existing_policy: dict) -> dict:
    if not isinstance(auto_policy, dict) or not isinstance(existing_policy, dict):
        return auto_policy
    trade_date = str(auto_policy.get("trade_date") or "")
    if not trade_date or str(existing_policy.get("trade_date") or "") != trade_date:
        return auto_policy
    overrides = existing_policy.get("watchdog_overrides")
    if not isinstance(overrides, dict):
        return auto_policy

    active_base = auto_policy.get("active_base") if isinstance(auto_policy.get("active_base"), dict) else {}
    merged = merge_policy_views(active_base, overrides)

    payload = dict(auto_policy)
    payload["active_base"] = dict(active_base)
    payload["watchdog_overrides"] = dict(overrides)
    payload["active"] = merged
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary.update(summarize_active_policy(merged))
    payload["summary"] = summary
    return payload
