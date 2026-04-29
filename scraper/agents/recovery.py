from __future__ import annotations

from typing import Any

from .models import RecoveryDecision


class FallbackRecoveryDecisionAgent:
    def decide(
        self,
        *,
        detail: dict[str, Any],
        current_status: str,
        error_stage: str,
        error_type: str,
        error_message: str,
        fallback_used: str,
    ) -> RecoveryDecision:
        page_type = detail.get("page_type", "")
        has_master_data = bool(detail.get("master_data"))

        if has_master_data:
            return RecoveryDecision(
                status=current_status,
                error_stage=error_stage,
                error_type=error_type,
                error_message=error_message,
                fallback_used=fallback_used,
            )

        if current_status == "success":
            if page_type == "no_item_options_page":
                return RecoveryDecision(
                    status="partial_fallback",
                    error_stage="recovery_decision",
                    error_type="no_item_options_available",
                    error_message=(
                        "Page indicates no options of this product are available; "
                        "wrote page-level fallback record."
                    ),
                    fallback_used="yes",
                )
            return RecoveryDecision(
                status="partial_fallback",
                error_stage="item_extraction",
                error_type="no_master_data",
                error_message="No item-level masterData found; wrote fallback record.",
                fallback_used="yes",
            )

        if not error_message:
            if page_type == "no_item_options_page":
                error_message = (
                    "Page indicates no options of this product are available; "
                    "wrote page-level fallback record."
                )
            else:
                error_message = "No item-level masterData found; wrote fallback record."

        return RecoveryDecision(
            status=current_status,
            error_stage=error_stage,
            error_type=error_type,
            error_message=error_message,
            fallback_used=fallback_used,
        )
