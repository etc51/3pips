from __future__ import annotations

import json

from autonomy_common import now_str, safe_float, safe_int
from auto_policy_utils import (
    merge_blackout_windows,
    merge_group_blackout_windows,
    normalize_blackout_windows,
    normalize_clock_hhmm,
    normalize_entry_shadow_gate_group_models,
    normalize_group_blackout_windows,
    normalize_shadow_model_name,
)


def _earlier_clock_hhmm(left: object, right: object) -> str:
    left_norm = normalize_clock_hhmm(left)
    right_norm = normalize_clock_hhmm(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    return left_norm if left_norm <= right_norm else right_norm


def _later_clock_hhmm(left: object, right: object) -> str:
    left_norm = normalize_clock_hhmm(left)
    right_norm = normalize_clock_hhmm(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    return left_norm if left_norm >= right_norm else right_norm


def _value_token(policy_key: str, value: object) -> str:
    if policy_key in {"entry_blackout_windows", "entry_blackout_group_windows", "entry_shadow_gate_group_models"}:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _policy_contains_value(active: dict, policy_key: str, value: object) -> bool:
    if not isinstance(active, dict):
        return False
    if policy_key == "entry_no_trade_before":
        active_value = normalize_clock_hhmm(active.get(policy_key))
        return bool(active_value and active_value >= normalize_clock_hhmm(value))
    if policy_key == "entry_no_new_after":
        active_value = normalize_clock_hhmm(active.get(policy_key))
        return bool(active_value and active_value <= normalize_clock_hhmm(value))
    if policy_key == "entry_blackout_windows":
        active_windows = set(normalize_blackout_windows(active.get(policy_key)))
        return set(normalize_blackout_windows(value)).issubset(active_windows)
    if policy_key == "entry_blackout_group_windows":
        active_groups = normalize_group_blackout_windows(active.get(policy_key))
        candidate_groups = normalize_group_blackout_windows(value)
        for group_key, windows in candidate_groups.items():
            if not set(windows).issubset(set(active_groups.get(group_key) or [])):
                return False
        return bool(candidate_groups)
    if policy_key == "entry_max_full_stop_rub":
        try:
            active_cap = int(active.get(policy_key))
            candidate_cap = int(value)
        except Exception:
            return False
        return active_cap <= candidate_cap
    if policy_key == "entry_shadow_gate_group_models":
        active_models = normalize_entry_shadow_gate_group_models(active.get(policy_key))
        candidate_models = normalize_entry_shadow_gate_group_models(value)
        if not candidate_models:
            return False
        return all(active_models.get(group_key) == model for group_key, model in candidate_models.items())
    return False


def build_candidate_entries(auto_policy: dict, trade_date: str, promote_after_days: int = 3) -> list[dict]:
    active = auto_policy.get("active_base") if isinstance(auto_policy.get("active_base"), dict) else {}
    proposed = auto_policy.get("proposed") if isinstance(auto_policy.get("proposed"), dict) else {}
    out: list[dict] = []

    def add_candidate(
        *,
        policy_key: str,
        value: object,
        anchor_key: str,
        note: str,
        min_total_delta_rub: float,
    ) -> None:
        anchor = str(proposed.get(anchor_key) or "").strip()
        if not anchor:
            return
        if policy_key == "entry_no_trade_before":
            value = normalize_clock_hhmm(value)
        elif policy_key == "entry_no_new_after":
            value = normalize_clock_hhmm(value)
        elif policy_key == "entry_blackout_windows":
            value = normalize_blackout_windows(value)
        elif policy_key == "entry_blackout_group_windows":
            value = normalize_group_blackout_windows(value)
        elif policy_key == "entry_max_full_stop_rub":
            try:
                value = int(value)
            except Exception:
                value = None
        if value in (None, "", [], {}):
            return
        if _policy_contains_value(active, policy_key, value):
            return
        out.append(
            {
                "candidate_id": f"{policy_key}|{anchor}|{_value_token(policy_key, value)}",
                "policy_key": policy_key,
                "value": value,
                "source_scenario": anchor,
                "scope": "new_entries_only",
                "created_trade_date": trade_date,
                "promote_after_days": int(promote_after_days),
                "min_total_delta_rub": float(min_total_delta_rub),
                "note": note,
            }
        )

    add_candidate(
        policy_key="entry_no_trade_before",
        value=proposed.get("candidate_entry_start"),
        anchor_key="candidate_entry_start_anchor",
        note="candidate later entry start from nightly research",
        min_total_delta_rub=1_000.0,
    )
    add_candidate(
        policy_key="entry_no_new_after",
        value=proposed.get("candidate_entry_cutoff"),
        anchor_key="candidate_entry_cutoff_anchor",
        note="candidate earlier entry cutoff from nightly research",
        min_total_delta_rub=1_000.0,
    )
    add_candidate(
        policy_key="entry_blackout_windows",
        value=proposed.get("candidate_entry_blackout_windows"),
        anchor_key="candidate_entry_blackout_anchor",
        note="candidate entry blackout windows from nightly research",
        min_total_delta_rub=1_500.0,
    )
    add_candidate(
        policy_key="entry_blackout_group_windows",
        value=proposed.get("candidate_group_blackout_windows"),
        anchor_key="candidate_group_blackout_anchor",
        note="candidate group blackout windows from nightly research",
        min_total_delta_rub=1_500.0,
    )
    add_candidate(
        policy_key="entry_max_full_stop_rub",
        value=proposed.get("candidate_stop_cap_rub"),
        anchor_key="candidate_stop_cap_anchor",
        note="candidate full-stop cap from nightly research",
        min_total_delta_rub=1_000.0,
    )
    for row in proposed.get("candidate_entry_shadow_gate_rows") or []:
        portfolio_group = str(row.get("portfolio_group") or "").strip().upper()
        contour = str(row.get("contour") or "").strip().upper()
        model = normalize_shadow_model_name(row.get("model"))
        if not portfolio_group or contour not in {"STRICT", "AGGRESSIVE"} or not model:
            continue
        value = {f"{portfolio_group}/{contour}": model}
        if _policy_contains_value(active, "entry_shadow_gate_group_models", value):
            continue
        anchor = str(row.get("candidate") or f"entry_shadow_gate::{portfolio_group}/{contour}/{model}")
        try:
            promote_after = max(1, int(row.get("promote_after_days") or 2))
        except Exception:
            promote_after = 2
        delta_floor = max(500.0, safe_float(row.get("min_total_delta_rub"), 0.0))
        out.append(
            {
                "candidate_id": f"entry_shadow_gate_group_models|{anchor}|{_value_token('entry_shadow_gate_group_models', value)}",
                "policy_key": "entry_shadow_gate_group_models",
                "value": value,
                "source_scenario": anchor,
                "scope": "new_entries_only",
                "created_trade_date": trade_date,
                "promote_after_days": promote_after,
                "min_total_delta_rub": delta_floor,
                "note": str(row.get("note") or f"candidate entry shadow gate {portfolio_group}/{contour} -> {model}"),
            }
        )
    return out


def evaluate_entry_shadow_candidate_days(candidate: dict, strategy_review_history: list[dict]) -> list[dict]:
    source_scenario = str(candidate.get("source_scenario") or "")
    created_trade_date = str(candidate.get("created_trade_date") or "")
    value = normalize_entry_shadow_gate_group_models(candidate.get("value"))
    if not created_trade_date or len(value) != 1:
        return []
    group_key, model_name = next(iter(value.items()))
    rows_by_date: dict[str, dict] = {}
    for row in strategy_review_history:
        trade_date = str(row.get("trade_date") or "")
        if not trade_date or trade_date <= created_trade_date:
            continue
        portfolio_group = str(row.get("portfolio_group") or "").strip().upper()
        contour = str(row.get("contour") or "").strip().upper()
        row_group_key = f"{portfolio_group}/{contour}" if portfolio_group and contour else ""
        row_model_name = normalize_shadow_model_name(row.get("model"))
        row_candidate = str(row.get("candidate") or "")
        if row_group_key != group_key or row_model_name != model_name:
            continue
        if source_scenario and row_candidate and row_candidate != source_scenario:
            continue
        delta = round(safe_float(row.get("delta_vs_base_rub")), 2)
        model_net = round(safe_float(row.get("model_net_rub")), 2)
        rows_by_date[trade_date] = {
            "trade_date": trade_date,
            "candidate_net_rub": model_net,
            "base_net_rub": round(model_net - delta, 2),
            "delta_rub": delta,
            "beat_base": delta > 0 and model_net > 0,
        }
    return [rows_by_date[key] for key in sorted(rows_by_date)]


def evaluate_candidate_days(candidate: dict, scenario_history: list[dict], strategy_review_history: list[dict] | None = None) -> list[dict]:
    if str(candidate.get("policy_key") or "") == "entry_shadow_gate_group_models":
        return evaluate_entry_shadow_candidate_days(candidate, strategy_review_history or [])
    source_scenario = str(candidate.get("source_scenario") or "")
    created_trade_date = str(candidate.get("created_trade_date") or "")
    if not source_scenario or not created_trade_date:
        return []
    base_by_date = {
        str(row.get("trade_date") or ""): row
        for row in scenario_history
        if str(row.get("scenario") or "") == "base" and str(row.get("trade_date") or "") > created_trade_date
    }
    scenario_by_date = {
        str(row.get("trade_date") or ""): row
        for row in scenario_history
        if str(row.get("scenario") or "") == source_scenario and str(row.get("trade_date") or "") > created_trade_date
    }
    out: list[dict] = []
    for trade_date in sorted(set(base_by_date) & set(scenario_by_date)):
        base_row = base_by_date[trade_date]
        candidate_row = scenario_by_date[trade_date]
        delta = round(safe_float(candidate_row.get("net_rub")) - safe_float(base_row.get("net_rub")), 2)
        out.append(
            {
                "trade_date": trade_date,
                "candidate_net_rub": round(safe_float(candidate_row.get("net_rub")), 2),
                "base_net_rub": round(safe_float(base_row.get("net_rub")), 2),
                "delta_rub": delta,
                "beat_base": delta > 0,
            }
        )
    return out


def advance_candidate_gate(
    existing_state: dict,
    auto_policy: dict,
    scenario_history: list[dict],
    trade_date: str,
    strategy_review_history: list[dict] | None = None,
) -> dict:
    existing_state = existing_state if isinstance(existing_state, dict) else {}
    pending_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in (existing_state.get("pending") or [])
        if str(item.get("candidate_id") or "")
    }
    promoted_history = list(existing_state.get("promoted_history") or [])
    rejected_history = list(existing_state.get("rejected_history") or [])
    resolved_ids = {
        str(item.get("candidate_id") or "")
        for item in promoted_history + rejected_history
        if str(item.get("candidate_id") or "")
    }
    current_candidates = build_candidate_entries(auto_policy, trade_date)
    current_ids = {str(item.get("candidate_id") or "") for item in current_candidates}

    pending: list[dict] = []
    promoted_now: list[dict] = []
    rejected_now: list[dict] = []

    for candidate_id, previous in pending_by_id.items():
        if candidate_id in current_ids:
            continue
        payload = dict(previous)
        payload["status"] = "rejected"
        payload["resolved_trade_date"] = trade_date
        payload["reason"] = "candidate_no_longer_proposed"
        rejected_history.append(payload)
        rejected_now.append(payload)

    for current in current_candidates:
        candidate_id = str(current.get("candidate_id") or "")
        if not candidate_id:
            continue
        if candidate_id in resolved_ids:
            continue
        previous = pending_by_id.get(candidate_id, {})
        evaluations_by_date = {
            str(item.get("trade_date") or ""): item
            for item in (previous.get("evaluations") or [])
            if str(item.get("trade_date") or "")
        }
        for item in evaluate_candidate_days(previous or current, scenario_history, strategy_review_history):
            evaluations_by_date.setdefault(str(item.get("trade_date") or ""), item)
        evaluations = [evaluations_by_date[key] for key in sorted(evaluations_by_date)]
        evaluation_days = len(evaluations)
        beat_days = sum(1 for item in evaluations if bool(item.get("beat_base")))
        total_delta = round(sum(safe_float(item.get("delta_rub")) for item in evaluations), 2)
        required_days = max(1, safe_int(previous.get("promote_after_days") or current.get("promote_after_days"), 3))
        min_total_delta_rub = safe_float(previous.get("min_total_delta_rub") or current.get("min_total_delta_rub"), 0.0)
        if any(not bool(item.get("beat_base")) for item in evaluations):
            payload = dict(current)
            payload.update(
                {
                    "status": "rejected",
                    "resolved_trade_date": trade_date,
                    "reason": "beat_base_failed",
                    "evaluation_days": evaluation_days,
                    "beat_base_days": beat_days,
                    "total_delta_rub": total_delta,
                    "evaluations": evaluations,
                }
            )
            rejected_history.append(payload)
            rejected_now.append(payload)
            continue
        if evaluation_days >= required_days and total_delta >= min_total_delta_rub:
            payload = dict(current)
            payload.update(
                {
                    "status": "promoted",
                    "resolved_trade_date": trade_date,
                    "reason": "promote_after_consistent_future_days",
                    "evaluation_days": evaluation_days,
                    "beat_base_days": beat_days,
                    "total_delta_rub": total_delta,
                    "evaluations": evaluations,
                }
            )
            promoted_history.append(payload)
            promoted_now.append(payload)
            continue
        payload = dict(current)
        payload.update(
            {
                "created_trade_date": str(previous.get("created_trade_date") or current.get("created_trade_date") or trade_date),
                "status": "pending",
                "evaluation_days": evaluation_days,
                "beat_base_days": beat_days,
                "total_delta_rub": total_delta,
                "evaluations": evaluations,
            }
        )
        pending.append(payload)

    return {
        "trade_date": trade_date,
        "updated_at": now_str(),
        "pending": pending,
        "promoted_history": promoted_history[-20:],
        "rejected_history": rejected_history[-20:],
        "promoted_now": promoted_now,
        "rejected_now": rejected_now,
        "summary": summarize_candidate_gate(
            {
                "pending": pending,
                "promoted_history": promoted_history,
                "rejected_history": rejected_history,
                "promoted_now": promoted_now,
                "rejected_now": rejected_now,
            }
        ),
    }


def apply_promoted_candidates(active: dict, promoted_candidates: list[dict]) -> dict:
    payload = dict(active) if isinstance(active, dict) else {}
    notes = [str(item) for item in (payload.get("notes") or []) if str(item).strip()]
    for candidate in promoted_candidates:
        policy_key = str(candidate.get("policy_key") or "")
        value = candidate.get("value")
        evaluation_days = safe_int(candidate.get("evaluation_days"), 0)
        total_delta_rub = round(safe_float(candidate.get("total_delta_rub")), 2)
        source_scenario = str(candidate.get("source_scenario") or "")
        if policy_key == "entry_no_trade_before":
            payload[policy_key] = _later_clock_hhmm(payload.get(policy_key), value)
        elif policy_key == "entry_no_new_after":
            payload[policy_key] = _earlier_clock_hhmm(payload.get(policy_key), value)
        elif policy_key == "entry_blackout_windows":
            payload[policy_key] = merge_blackout_windows(payload.get(policy_key), value)
        elif policy_key == "entry_blackout_group_windows":
            payload[policy_key] = merge_group_blackout_windows(payload.get(policy_key), value)
        elif policy_key == "entry_max_full_stop_rub":
            try:
                current_value = int(payload.get(policy_key))
            except Exception:
                current_value = None
            try:
                candidate_value = int(value)
            except Exception:
                candidate_value = None
            if candidate_value is not None:
                if current_value is None:
                    payload[policy_key] = candidate_value
                else:
                    payload[policy_key] = min(current_value, candidate_value)
        elif policy_key == "entry_shadow_gate_group_models":
            current_models = normalize_entry_shadow_gate_group_models(payload.get(policy_key))
            candidate_models = normalize_entry_shadow_gate_group_models(value)
            if candidate_models:
                current_models.update(candidate_models)
                payload[policy_key] = {key: current_models[key] for key in sorted(current_models)}
        else:
            continue
        notes.append(
            f"Candidate gate: {policy_key} активирован из {source_scenario} после {evaluation_days} подтвержд. дн. и суммарного delta {total_delta_rub:.2f} ₽."
        )
    payload["notes"] = list(dict.fromkeys(notes))[:12]
    return payload


def promoted_runtime_candidates(state: dict) -> list[dict]:
    if not isinstance(state, dict):
        return []
    by_id: dict[str, dict] = {}
    for item in state.get("promoted_candidates") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            continue
        by_id[candidate_id] = dict(item)
    return [by_id[key] for key in sorted(by_id)]


def summarize_promoted_runtime_policy_state(state: dict) -> dict[str, object]:
    candidates = promoted_runtime_candidates(state)
    policy_keys = sorted({str(item.get("policy_key") or "") for item in candidates if str(item.get("policy_key") or "")})
    return {
        "promoted_candidate_count": len(candidates),
        "policy_keys": policy_keys,
        "top_candidate": candidates[0] if candidates else {},
    }


def build_promoted_runtime_policy_state(existing_state: dict, promoted_now: list[dict], trade_date: str) -> dict:
    by_id = {
        str(item.get("candidate_id") or ""): dict(item)
        for item in promoted_runtime_candidates(existing_state)
        if str(item.get("candidate_id") or "")
    }
    for candidate in promoted_now:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            continue
        payload = dict(by_id.get(candidate_id) or {})
        payload.update(dict(candidate))
        payload["candidate_id"] = candidate_id
        payload["promoted_trade_date"] = str(
            payload.get("promoted_trade_date")
            or candidate.get("resolved_trade_date")
            or candidate.get("created_trade_date")
            or trade_date
        )
        payload["last_confirmed_trade_date"] = trade_date
        by_id[candidate_id] = payload

    candidates = [by_id[key] for key in sorted(by_id)]
    state = {
        "trade_date": trade_date,
        "updated_at": now_str(),
        "promoted_candidates": candidates[-50:],
    }
    state["active_base"] = apply_promoted_candidates({}, state["promoted_candidates"])
    state["summary"] = summarize_promoted_runtime_policy_state(state)
    return state


def summarize_candidate_gate(state: dict) -> dict[str, object]:
    pending = list(state.get("pending") or [])
    promoted_now = list(state.get("promoted_now") or [])
    rejected_now = list(state.get("rejected_now") or [])
    promoted_history = list(state.get("promoted_history") or [])
    rejected_history = list(state.get("rejected_history") or [])
    return {
        "pending_count": len(pending),
        "promoted_now_count": len(promoted_now),
        "rejected_now_count": len(rejected_now),
        "promoted_total": len(promoted_history),
        "rejected_total": len(rejected_history),
        "top_pending": pending[0] if pending else {},
        "top_promoted_now": promoted_now[0] if promoted_now else {},
    }
