"""Execution-specific supplemental models.

Domain models (InternalOrder, TradeFill, Position, RiskDecision) live in
trading_engine.domain.models.  This module holds any execution-layer models
that are not part of the core domain vocabulary.

Currently a thin module — extended in future milestones.
"""

from __future__ import annotations
