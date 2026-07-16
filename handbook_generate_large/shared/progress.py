# -*- coding: utf-8 -*-
"""progress.py — tiny progress + ETA helper shared across phases.

Each phase that does countable work wraps it in a Progress: every tick logs
``[label done/total] note · elapsed · ETA``. ETA is a linear extrapolation from
the average time per unit so far, so it is rough for uneven work (use weighted
ticks — advance by a bucket's function count, not 1 — when units differ a lot).
"""
from __future__ import annotations

import logging
import time


def fmt_dur(secs: float) -> str:
    """Compact wall-clock: '12s' / '3m05s' / '1h02m'."""
    secs = max(0.0, secs)
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{int(secs // 60)}m{int(secs % 60):02d}s"
    return f"{int(secs // 3600)}h{int((secs % 3600) // 60):02d}m"


class Progress:
    """Count-and-ETA tracker. total/done are in arbitrary units (items or a
    weight like function-count); pass weight= to tick to advance by more than 1."""

    def __init__(self, logger: logging.Logger, label: str, total: float) -> None:
        self.logger = logger
        self.label = label
        self.total = max(0.0, float(total))
        self.done = 0.0
        self.t0 = time.time()

    def tick(self, weight: float = 1.0, note: str = "") -> None:
        self.done += weight
        elapsed = time.time() - self.t0
        frac = self.done / self.total if self.total else 1.0
        eta = (elapsed / self.done) * (self.total - self.done) if self.done > 0 else 0.0
        self.logger.info(
            "[%s %d/%d · %d%%] %s· elapsed %s · ETA %s",
            self.label, int(self.done), int(self.total), int(frac * 100),
            (note + " ") if note else "", fmt_dur(elapsed), fmt_dur(eta),
        )

    def finish(self) -> None:
        self.logger.info("[%s done] %d unit(s) in %s",
                         self.label, int(self.total), fmt_dur(time.time() - self.t0))
