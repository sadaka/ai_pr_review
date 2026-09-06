"""Minimal async Redis-backed LangGraph checkpointer.

Scope is deliberately narrow: "a killed worker can resume a run without
re-running completed nodes" (M3's actual requirement), not full checkpoint
time-travel / delta-optimized storage. Every checkpoint is stored whole
(`channel_values` inlined, no blob/version dedup scheme) — simpler and
sufficient for a handful of small stub-node states; revisit if state size
becomes a real concern.

Only the async (`a*`) methods are implemented — the orchestrator is async
end to end (ARQ workers, FastAPI), so the sync interface is intentionally
unsupported here (raises NotImplementedError, matching the base contract for
an unimplemented method).
"""
from __future__ import annotations

import pickle
from collections.abc import AsyncIterator, Sequence
from typing import Any, Awaitable, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from redis.asyncio import Redis

_PREFIX = "orchestrator:checkpoint"
_FIELD_SEP = "\x1f"  # unit separator — safe against task_id containing ':' etc.


def _checkpoint_key(thread_id: str, ns: str, checkpoint_id: str) -> str:
    return f"{_PREFIX}:cp:{thread_id}:{ns}:{checkpoint_id}"


def _order_key(thread_id: str, ns: str) -> str:
    return f"{_PREFIX}:order:{thread_id}:{ns}"


def _writes_key(thread_id: str, ns: str, checkpoint_id: str) -> str:
    return f"{_PREFIX}:writes:{thread_id}:{ns}:{checkpoint_id}"


_T = TypeVar("_T")


def _a(value: Any) -> Awaitable[_T]:
    """redis-py types its command methods as `Awaitable[T] | T` because the
    same client class also backs sync pipelines; every call in this module is
    against an async client, so this cast just tells mypy what's already true
    at runtime — no behavior change."""
    return cast(Awaitable[_T], value)


class RedisSaver(BaseCheckpointSaver[str]):
    """Checkpoint saver backed by plain Redis (no RediSearch / module
    dependency — works against any Redis, including Upstash's free tier)."""

    def __init__(self, redis: Redis, *, serde: Any = None) -> None:
        super().__init__(serde=serde)
        self._redis = redis

    async def _latest_checkpoint_id(self, thread_id: str, ns: str) -> str | None:
        last: bytes | None = await _a(self._redis.lindex(_order_key(thread_id, ns), -1))
        return last.decode() if last else None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            checkpoint_id = await self._latest_checkpoint_id(thread_id, ns)
            if checkpoint_id is None:
                return None

        raw: bytes | None = await _a(self._redis.get(_checkpoint_key(thread_id, ns, checkpoint_id)))
        if raw is None:
            return None
        entry = pickle.loads(raw)
        checkpoint: Checkpoint = self.serde.loads_typed(entry["checkpoint"])
        metadata: CheckpointMetadata = self.serde.loads_typed(entry["metadata"])
        parent_id: str | None = entry["parent_id"]

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            pending_writes=await self._load_pending_writes(thread_id, ns, checkpoint_id),
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent_id}}
                if parent_id
                else None
            ),
        )

    async def _load_pending_writes(self, thread_id: str, ns: str, checkpoint_id: str) -> list:
        raw_fields: dict[Any, Any] = await _a(self._redis.hgetall(_writes_key(thread_id, ns, checkpoint_id)))
        entries = []
        for field, blob in raw_fields.items():
            field_str = field.decode() if isinstance(field, bytes) else field
            _, idx_str = field_str.rsplit(_FIELD_SEP, 1)
            task_id, channel, value_blob, _task_path = pickle.loads(blob)
            entries.append((int(idx_str), task_id, channel, self.serde.loads_typed(value_blob)))
        entries.sort(key=lambda e: e[0])
        return [(task_id, channel, value) for _idx, task_id, channel, value in entries]

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return  # listing across all threads isn't needed by this project; scope to one thread
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        before_id = get_checkpoint_id(before) if before else None

        raw_ids: list[bytes] = await _a(self._redis.lrange(_order_key(thread_id, ns), 0, -1))
        ids = [cid.decode() for cid in raw_ids]
        ids.reverse()  # newest first, matching InMemorySaver's ordering
        count = 0
        for checkpoint_id in ids:
            if before_id and checkpoint_id >= before_id:
                continue
            tup = await self.aget_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}
            )
            if tup is None:
                continue
            if filter and not all(v == tup.metadata.get(k) for k, v in filter.items()):
                continue
            yield tup
            count += 1
            if limit is not None and count >= limit:
                return

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        entry = {
            "checkpoint": self.serde.dumps_typed(checkpoint),
            "metadata": self.serde.dumps_typed(get_checkpoint_metadata(config, metadata)),
            "parent_id": parent_id,
        }
        await _a(self._redis.set(_checkpoint_key(thread_id, ns, checkpoint_id), pickle.dumps(entry)))
        await _a(self._redis.rpush(_order_key(thread_id, ns), checkpoint_id))

        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        key = _writes_key(thread_id, ns, checkpoint_id)

        raw_keys: list[Any] = await _a(self._redis.hkeys(key))
        existing_fields = {f.decode() if isinstance(f, bytes) else f for f in raw_keys}
        to_set: dict[str, bytes] = {}
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            field = f"{task_id}{_FIELD_SEP}{write_idx}"
            if write_idx >= 0 and field in existing_fields:
                continue  # matches InMemorySaver: regular (non-special) writes are write-once per (task, idx)
            to_set[field] = pickle.dumps((task_id, channel, self.serde.dumps_typed(value), task_path))
        if to_set:
            await _a(self._redis.hset(key, mapping=to_set))

    async def adelete_thread(self, thread_id: str) -> None:
        pattern = f"{_PREFIX}:*:{thread_id}:*"
        cursor = 0
        while True:
            scan_result: tuple[int, list[Any]] = await _a(self._redis.scan(cursor=cursor, match=pattern, count=200))
            cursor, keys = scan_result
            if keys:
                await _a(self._redis.delete(*keys))
            if cursor == 0:
                break
