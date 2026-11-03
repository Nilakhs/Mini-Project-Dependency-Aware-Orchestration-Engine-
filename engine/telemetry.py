"""
E4 — Build Telemetry Engine Facade

Exposes telemetry components to the build engine and external consumers.
"""

from telemetry.collector import TelemetryCollector
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent, TelemetryEventType
from telemetry.report import BenchmarkComparison, calculate_comparison

__all__ = [
    "TelemetryEvent",
    "TelemetryEventType",
    "EventBus",
    "TelemetryDB",
    "TelemetryCollector",
    "BenchmarkComparison",
    "calculate_comparison",
]
