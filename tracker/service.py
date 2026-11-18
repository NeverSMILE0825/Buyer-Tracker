"""Resolve a time or block range, then build the transfer table."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from tracker.analyze import build_rows
from tracker.cache import collect_records
from tracker.checksum import normalize_address
from tracker.db import db
from tracker.etherscan import EtherscanError, KeyPool
from tracker.formatutil import clean_label

GENESIS_TS = 1_438_269_973
# Share of the bar each phase fills. Token transfers are the long scan.
# ETH and internal transactions run together, so they share one slice.
RESOLVE_FRACTION = 0.05
TOKEN_FRACTION = 0.65
ETH_FRACTION = 0.26
BUILD_FRACTION = 0.04
Progress = Callable[[str, float, dict[str, Any]], Awaitable[None]]


def block_fraction(reached: int | None, start: int, end: int) -> float:
    """How much of the inclusive block span [start, end] is covered through `reached`."""
    width = end - start + 1
    if width <= 0:
        return 1.0
    if reached is None or reached < start:
        return 0.0
    covered = min(end, reached) - start + 1
    return min(1.0, max(0.0, covered / width))


class _FetchProgress:
    """Turn block-cursor updates into one monotonic fraction of the whole fetch."""

    def __init__(self, progress: Progress, started: float, keys: int) -> None:
        self._progress = progress
        self._started = started
        self._lock = asyncio.Lock()
        self.keys = keys
        self.fraction = 0.0
        self.token_rows = 0
        self.token_blocks = 0
        self.tx_rows = 0
        self.internal_rows = 0
        self.tx_fraction = 0.0
        self.internal_fraction = 0.0

    async def send(self, message: str, fraction: float, **extra: Any) -> None:
        fraction = min(1.0, max(self.fraction, fraction))
        self.fraction = fraction
        payload = {key: value for key, value in extra.items() if value is not None}
        payload["elapsed_ms"] = int((time.perf_counter() - self._started) * 1000)
        if self.keys > 1:
            payload["keys"] = self.keys
        await self._progress(message, round(fraction, 4), payload)

    async def token(self, rows: int, blocks_done: int, blocks_total: int, start: int, end: int) -> None:
        async with self._lock:
            self.token_rows = max(self.token_rows, rows)
            self.token_blocks = max(self.token_blocks, blocks_done)
            phase = 1.0 if blocks_total <= 0 else min(1.0, self.token_blocks / blocks_total)
            await self.send(
                "Reading token transfers",
                RESOLVE_FRACTION + TOKEN_FRACTION * phase,
                rows=self.token_rows,
                blocks_done=self.token_blocks,
                blocks_total=blocks_total,
                start_block=start,
                end_block=end,
            )

    async def eth(self, kind: str, rows: int, blocks_done: int, blocks_total: int, start: int, end: int) -> None:
        async with self._lock:
            phase = 1.0 if blocks_total <= 0 else min(1.0, max(0, blocks_done) / blocks_total)
            if kind == "tx":
                self.tx_rows = max(self.tx_rows, rows)
                self.tx_fraction = max(self.tx_fraction, phase)
            else:
                self.internal_rows = max(self.internal_rows, rows)
                self.internal_fraction = max(self.internal_fraction, phase)
            combined = (self.tx_fraction + self.internal_fraction) / 2
            await self.send(
                "Reading ETH and internal transactions",
                RESOLVE_FRACTION + TOKEN_FRACTION + ETH_FRACTION * combined,
                detail=f"ETH {self.tx_rows:,} rows · internal {self.internal_rows:,} rows",
                start_block=start,
                end_block=end,
            )


async def track_many(
    client: KeyPool,
    addresses: list[str],
    label: str,
    mode: str,
    start_time: int | None,
    end_time: int | None,
    start_block: int | None,
    end_block: int | None,
    progress: Progress,
) -> dict[str, Any]:
    """Load every selected wallet for the same range and return one table."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in addresses:
        wallet = normalize_address(raw)
        if wallet.lower() in seen:
            continue
        seen.add(wallet.lower())
        ordered.append(wallet)
    if not ordered:
        raise ValueError("Turn on a saved wallet, or enter an address.")
    if len(ordered) == 1:
        saved = db.get_wallet(ordered[0])
        return await track(
            client,
            ordered[0],
            saved["label"] if saved and saved["label"] else label,
            mode,
            start_time,
            end_time,
            start_block,
            end_block,
            progress,
        )

    started = time.perf_counter()
    calls_before = client.calls
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    cached_blocks = 0
    fetched_blocks = 0
    wallets: list[dict[str, str]] = []
    start = end = used_start = used_end = None
    count = len(ordered)
    for index, wallet in enumerate(ordered):
        saved = db.get_wallet(wallet)
        wallet_label = str(saved["label"]) if saved and saved["label"] else ""
        name = wallet_label or f"{wallet[:6]}…{wallet[-4:]}"

        async def scaled(
            message: str,
            fraction: float,
            extra: dict[str, Any],
            index: int = index,
            name: str = name,
        ) -> None:
            payload = dict(extra or {})
            payload["wallet_index"] = index + 1
            payload["wallet_count"] = count
            await progress(f"{name} · {message}", (index + float(fraction)) / count, payload)

        payload = await track(
            client,
            wallet,
            wallet_label,
            mode,
            start_time,
            end_time,
            start_block,
            end_block,
            scaled,
        )
        meta = payload["meta"]
        rows.extend(payload["rows"])
        for warning in meta["warnings"]:
            warnings.append(f"{name}: {warning}")
        cached_blocks += int(meta["cached_blocks"])
        fetched_blocks += int(meta["fetched_blocks"])
        wallets.append({"address": meta["address"], "label": meta["label"]})
        if meta["start_block"] is not None:
            start = meta["start_block"]
            end = meta["end_block"]
            used_start = meta["start_time"]
            used_end = meta["end_time"]
    for seq, row in enumerate(rows):
        row["seq"] = seq
    combined = _payload(
        ordered[0],
        wallets[0]["label"],
        mode,
        start,
        end,
        used_start if used_start is not None else start_time,
        used_end if used_end is not None else end_time,
        rows,
        warnings,
        client.calls - calls_before,
        started,
        client.key_count,
        cached_blocks,
        fetched_blocks,
    )
    combined["meta"]["wallets"] = wallets
    return combined


async def track(
    client: KeyPool,
    address: str,
    label: str,
    mode: str,
    start_time: int | None,
    end_time: int | None,
    start_block: int | None,
    end_block: int | None,
    progress: Progress,
) -> dict[str, Any]:
    started = time.perf_counter()
    report = _FetchProgress(progress, started, client.key_count)
    wallet = normalize_address(address)
    saved = db.get_wallet(wallet)
    if saved is not None:
        wallet_label = saved["label"]
    else:
        wallet_label = clean_label(label)

    await report.send("Resolving the block range", 0.02)
    resolved = await resolve_range(client, mode, start_time, end_time, start_block, end_block)
    if resolved is None:
        await report.send("No blocks in that range", 1)
        return _payload(
            wallet,
            wallet_label,
            mode,
            None,
            None,
            start_time,
            end_time,
            [],
            ["No blocks were mined in that time range."],
            client.calls,
            started,
            client.key_count,
        )
    start, end, used_start, used_end = resolved
    head = await client.latest_block()
    await report.send(
        "Reading token transfers",
        RESOLVE_FRACTION,
        rows=0,
        start_block=start,
        end_block=end,
    )

    async def token_progress(count: int, blocks_done: int, blocks_total: int) -> None:
        await report.token(count, blocks_done, blocks_total, start, end)

    transfers, warnings, cached_blocks, fetched_blocks = await collect_records(
        db, client, wallet, "tokentx", start, end, head, token_progress
    )
    if not transfers:
        await report.send(
            "No token transfers in this range",
            1,
            rows=0,
            start_block=start,
            end_block=end,
        )
        return _payload(
            wallet,
            wallet_label,
            mode,
            start,
            end,
            used_start,
            used_end,
            [],
            warnings,
            client.calls,
            started,
            client.key_count,
            cached_blocks,
            fetched_blocks,
        )

    blocks = [int(item["blockNumber"]) for item in transfers if item.get("blockNumber")]
    span_start = min(blocks) if blocks else start
    span_end = max(blocks) if blocks else end
    await report.send(
        "Reading ETH and internal transactions",
        RESOLVE_FRACTION + TOKEN_FRACTION,
        detail="ETH 0 rows · internal 0 rows",
        start_block=span_start,
        end_block=span_end,
    )

    async def tx_progress(count: int, blocks_done: int, blocks_total: int) -> None:
        await report.eth("tx", count, blocks_done, blocks_total, span_start, span_end)

    async def internal_progress(count: int, blocks_done: int, blocks_total: int) -> None:
        await report.eth("internal", count, blocks_done, blocks_total, span_start, span_end)

    tx_task = collect_records(
        db, client, wallet, "txlist", span_start, span_end, head, tx_progress
    )
    internal_task = collect_records(
        db, client, wallet, "txlistinternal", span_start, span_end, head, internal_progress
    )
    (transactions, tx_warnings, _, _), (internals, internal_warnings, _, _) = await asyncio.gather(
        tx_task, internal_task
    )
    warnings.extend(tx_warnings)
    warnings.extend(internal_warnings)

    await report.send(
        "Building the table",
        RESOLVE_FRACTION + TOKEN_FRACTION + ETH_FRACTION + BUILD_FRACTION,
        rows=len(transfers),
        start_block=start,
        end_block=end,
    )
    rows, row_warnings = build_rows(
        wallet,
        wallet_label,
        db.label_map(),
        transfers,
        transactions,
        internals,
    )
    warnings.extend(row_warnings)
    return _payload(
        wallet,
        wallet_label,
        mode,
        start,
        end,
        used_start,
        used_end,
        rows,
        warnings,
        client.calls,
        started,
        client.key_count,
        cached_blocks,
        fetched_blocks,
    )


async def resolve_range(
    client: KeyPool,
    mode: str,
    start_time: int | None,
    end_time: int | None,
    start_block: int | None,
    end_block: int | None,
) -> tuple[int, int, int | None, int | None] | None:
    if mode == "block":
        if start_block is None or end_block is None:
            raise ValueError("Enter a start and end block.")
        if start_block < 0 or end_block < 0:
            raise ValueError("Block numbers must be zero or greater.")
        if start_block > end_block:
            raise ValueError("Start block must be less than or equal to the end block.")
        return start_block, end_block, None, None
    if mode != "time":
        raise ValueError("Choose a time range or a block range.")
    if start_time is None or end_time is None:
        raise ValueError("Choose a start and end time.")
    if start_time > end_time:
        raise ValueError("Start time must be before the end time.")
    if end_time < GENESIS_TS:
        return None
    start_time = max(start_time, GENESIS_TS)
    start_block = await client.block_by_time(start_time, "after")
    end_block = await client.block_by_time(end_time, "before")
    if start_block > end_block:
        return None
    return start_block, end_block, start_time, end_time


def _payload(
    wallet: str,
    label: str,
    mode: str,
    start_block: int | None,
    end_block: int | None,
    start_time: int | None,
    end_time: int | None,
    rows: list[dict[str, Any]],
    warnings: list[str],
    api_calls: int,
    started: float,
    keys: int = 1,
    cached_blocks: int = 0,
    fetched_blocks: int = 0,
) -> dict[str, Any]:
    tokens = len({row["token_contract"].lower() for row in rows}) if rows else 0
    return {
        "rows": rows,
        "meta": {
            "address": wallet,
            "label": label,
            "wallets": [{"address": wallet, "label": label}],
            "mode": mode,
            "start_block": start_block,
            "end_block": end_block,
            "start_time": start_time,
            "end_time": end_time,
            "transfers": len(rows),
            "tokens": tokens,
            "api_calls": api_calls,
            "keys": keys,
            "cached_blocks": cached_blocks,
            "fetched_blocks": fetched_blocks,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "warnings": [item for item in warnings if item],
        },
    }


def require_api_keys() -> list[str]:
    keys = db.enabled_api_keys()
    if keys:
        return keys
    if db.api_keys():
        raise EtherscanError("Enable an Etherscan API key in the sidebar first.")
    raise EtherscanError("Add an Etherscan API key in the sidebar first.")
