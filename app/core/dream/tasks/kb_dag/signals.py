from __future__ import annotations

from app.core.dream.event_bus import event_bus


def kb_maintenance_signal_count() -> float:
    """Information watermark for KB maintenance after node or file-index changes."""
    return float(
        event_bus.total_count("kb_nodes_added")
        + event_bus.total_count("file_indexed")
    )


def file_indexed_count() -> float:
    return float(event_bus.total_count("file_indexed"))
