"""Threshold alerts (brief page 2): user decides what 'unusual' means."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Alert:
    kind: str
    detail: str


def price_volume_alert(
    closes: list[float],
    volumes: list[float],
    price_threshold_pct: float = 5.0,
    volume_multiple: float = 2.0,
) -> Optional[Alert]:
    """Fire when the latest close moves beyond +/-threshold or the latest volume
    exceeds multiple x the average of the prior sessions. Defaults mirror the
    brief's examples; both are user-configurable."""
    if len(closes) < 2 or len(volumes) < 2:
        return None
    move = (closes[-1] - closes[-2]) / closes[-2] * 100.0
    if abs(move) >= price_threshold_pct:
        return Alert("price_move", f"{move:+.1f}% day move (threshold +/-{price_threshold_pct}%)")
    avg_vol = sum(volumes[:-1]) / (len(volumes) - 1)
    if avg_vol > 0 and volumes[-1] >= volume_multiple * avg_vol:
        return Alert("volume_spike", f"{volumes[-1] / avg_vol:.1f}x average volume (threshold {volume_multiple}x)")
    return None
