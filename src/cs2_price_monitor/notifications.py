from __future__ import annotations

from cs2_price_monitor.models import Alert


def format_alert(alert: Alert) -> str:
    """Format alert for future notification channels."""
    return f"[{alert.severity}] {alert.message}"
