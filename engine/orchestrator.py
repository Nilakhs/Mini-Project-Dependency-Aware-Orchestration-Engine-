"""
Build Orchestrator

Integrates the completed engine components:
- E1: BuildGraph (DAG)
- E2: Verifying-Trace Rebuilder & SQLite Cache
- E3: Topological Scheduler & AsyncProcessExecutor
- Provides an event callback hook for E4 Telemetry
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional, Union

from engine.cache import BuildCache
from engine.dag import BuildGraph
from engine.executor import AsyncProcessExecutor
from engine.ninja_parser import NinjaParser
from engine.rebuilder import Rebuilder
from engine.scheduler import BuildResult, TaskRecord, TopologicalScheduler


class Orchestrator:
    """
    Main entry point for running builds.
    Coordinates graph ingestion, rebuilder decisions, scheduling, and execution.
    """

    def __init__(
        self,
        manifest_path: str,
        db_path: str = "data/build_state.db",
        base_dir: Optional[str] = None,
        max_workers: int = 4,
        on_event: Optional[Callable[[str, TaskRecord], None]] = None,
    ):
        self.manifest_path = os.path.abspath(manifest_path)
        self.base_dir = base_dir or os.path.dirname(self.manifest_path)
        self.db_path = db_path
        self.max_workers = max_workers
        self.on_event = on_event

        # 1. Parse manifest
        self.parser = NinjaParser()
        self.manifest = self.parser.parse_file(self.manifest_path)

        # 2. Build DAG (E1)
        self.graph = BuildGraph.from_manifest(
            self.manifest, parser=self.parser, base_dir=self.base_dir
        )

        # 3. Initialize Cache & Rebuilder (E2)
        self.cache = BuildCache(db_path=self.db_path)
        self.rebuilder = Rebuilder(cache=self.cache, base_dir=self.base_dir)

        # 4. Executor & Scheduler (E3)
        self.executor = AsyncProcessExecutor(
            max_workers=max_workers, base_dir=self.base_dir
        )
        self.scheduler = TopologicalScheduler(
            graph=self.graph,
            rebuilder=self.rebuilder,
            executor=self.executor,
            max_workers=max_workers,
            on_event=self.on_event,
        )

    async def build(
        self, target: Optional[Union[str, list[str]]] = None
    ) -> BuildResult:
        """Execute a build session for the requested target(s)."""
        return await self.scheduler.build(target=target)

    def close(self) -> None:
        """Cleanly close database connections and resources."""
        self.cache.close()
