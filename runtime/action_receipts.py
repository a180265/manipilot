"""Physical driver receipts must be confirmed independently of plugin summaries."""


def motion_receipt_unknown(result) -> bool:
    if not isinstance(result, dict):
        return True
    if any(result.get(key) == "unknown" for key in (
            "outcome_hint", "outcome", "controller_status", "mechanical_status")):
        return True
    if result.get("motion_not_started") is True:
        return False
    confirmed = (result.get("ok") is True
                 or ("ok" not in result and result.get("controller_completed") is True))
    return not confirmed or result.get("controller_completed") is False
