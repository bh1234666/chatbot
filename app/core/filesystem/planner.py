"""Read/write planning helpers built on top of the file registry."""

from __future__ import annotations

from dataclasses import dataclass

from .models import FileRecord
from .registry import FileRegistry


@dataclass(slots=True)
class ReadShard:
    shard_id: str
    category: str
    project_paths: list[str]
    estimated_bytes: int


def build_read_plan(registry: FileRegistry, *, max_files_per_shard: int = 12, max_bytes_per_shard: int = 8_000_000) -> list[ReadShard]:
    records = [
        record
        for record in registry.list_records()
        if record.project_path and record.file_id != "__index_summary__"
    ]
    buckets: dict[str, list[FileRecord]] = {}
    for record in records:
        buckets.setdefault(record.category, []).append(record)

    shards: list[ReadShard] = []
    for category, items in sorted(buckets.items()):
        current: list[str] = []
        current_bytes = 0
        shard_idx = 1
        for record in sorted(items, key=lambda r: r.project_path):
            size = int(record.size or 0)
            if current and (len(current) >= max_files_per_shard or current_bytes + size > max_bytes_per_shard):
                shards.append(ReadShard(
                    shard_id=f"{category}_{shard_idx}",
                    category=category,
                    project_paths=current,
                    estimated_bytes=current_bytes,
                ))
                shard_idx += 1
                current = []
                current_bytes = 0
            current.append(record.project_path)
            current_bytes += size
        if current:
            shards.append(ReadShard(
                shard_id=f"{category}_{shard_idx}",
                category=category,
                project_paths=current,
                estimated_bytes=current_bytes,
            ))
    return shards
