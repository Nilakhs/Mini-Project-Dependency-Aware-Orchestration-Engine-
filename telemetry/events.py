"""
Telemetry Events & Data Models

Defines the structured event schema emitted by the build orchestration engine.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class TelemetryEventType(str, Enum):
    """Types of telemetry events emitted throughout a build lifecycle."""
    BUILD_STARTED = "BUILD_STARTED"
    TASK_READY = "TASK_READY"
    TASK_STARTED = "TASK_STARTED"
    TASK_SKIPPED = "TASK_SKIPPED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_BLOCKED = "TASK_BLOCKED"
    BUILD_COMPLETED = "BUILD_COMPLETED"
    BUILD_FAILED = "BUILD_FAILED"


@dataclass
class TelemetryEvent:
    """
    Structured telemetry event.
    
    Fields:
    - event_id: Unique sequential or prefixed identifier (e.g., 'evt_001')
    - build_id: Identifier of the build session (e.g., 'build_20261103_222500_abc123')
    - event_type: TelemetryEventType value
    - timestamp: Unix epoch timestamp in float seconds
    - iso_time: ISO-8601 UTC timestamp string
    - task_id: Identifier of the task (null for build-level events)
    - task_name: Human-readable task name or output file
    - worker_id: Worker slot number (1..N, 0 if not assigned)
    - status: Current task or build status string
    - duration: Elapsed duration in seconds (for completed/failed tasks or build summary)
    - message: Descriptive message, skip reason, or failure cause
    - stdout: Standard output excerpt from task execution
    - stderr: Standard error excerpt from task execution
    - metadata: Additional arbitrary key-value context
    """
    event_id: str
    build_id: str
    event_type: TelemetryEventType
    timestamp: float = field(default_factory=time.time)
    iso_time: str = ""
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    worker_id: Optional[int] = None
    status: Optional[str] = None
    duration: Optional[float] = None
    message: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.iso_time:
            self.iso_time = datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if isinstance(self.event_type, str):
            self.event_type = TelemetryEventType(self.event_type)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d
