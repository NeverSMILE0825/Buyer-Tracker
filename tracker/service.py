"""Resolve a time or block range, then build the transfer table."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from tracker.analyze import build_rows
from tracker.checksum import normalize_address
from tracker.db import db
from tracker.etherscan import EtherscanClient, EtherscanError
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

    def __init__(self, progress: Progress, started: float) -> None:
        self._progress = progress
        self._started = started
        self.fraction = 0.0
        self.token_rows = 0
        self.tx_rows = 0
        self.internal_rows = 0
        self.tx_fraction = 0.0
        self.internal_fraction = 0.0

    async def send(self, message: str, fraction: float, **extra: Any) -> None:
        fraction = min(1.0, max(self.fraction, fraction))
        self.fraction = fraction
        payload = {key: value for key, value in extra.items() if value is not None}
        payload["elapsed_ms"] = int((time.perf_counter() - self._started) * 1000)
        await self._progress(message, round(fraction, 4), payload)

    async def token(self, rows: int, reached: int, start: int, end: int) -> None:
        self.token_rows = max(self.token_rows, rows)
        phase = block_fraction(reached, start, end)
        await self.send(
            "Reading token transfers",
            RESOLVE_FRACTION + TOKEN_FRACTION * phase,
            rows=self.token_rows,
            block=min(end, max(start, reached)),
            start_block=start,
            end_block=end,
        )

    async def eth(self, kind: str, rows: int, reached: int, start: int, end: int) -> None:
        phase = block_fraction(reached, start, end)
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


async def track(
    client: EtherscanClient,
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
    report = _FetchProgress(progress, started)
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
        )
    start, end, used_start, used_end = resolved
    await report.send(
        "Reading token transfers",
        RESOLVE_FRACTION,
        rows=0,
        start_block=start,
        end_block=end,
    )

    async def token_progress(count: int, reached: int) -> None:
        await report.token(count, reached, start, end)

    transfers, warnings = await client.fetch_account(
        "tokentx", wallet, start, end, token_progress
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

    async def tx_progress(count: int, reached: int) -> None:
        await report.eth("tx", count, reached, span_start, span_end)

    async def internal_progress(count: int, reached: int) -> None:
        await report.eth("internal", count, reached, span_start, span_end)

    tx_task = client.fetch_account("txlist", wallet, span_start, span_end, tx_progress)
    internal_task = client.fetch_account(
        "txlistinternal", wallet, span_start, span_end, internal_progress
    )
    (transactions, tx_warnings), (internals, internal_warnings) = await asyncio.gather(
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
    )


async def resolve_range(
    client: EtherscanClient,
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
) -> dict[str, Any]:
    tokens = len({row["token_contract"].lower() for row in rows}) if rows else 0
    return {
        "rows": rows,
        "meta": {
            "address": wallet,
            "label": label,
            "mode": mode,
            "start_block": start_block,
            "end_block": end_block,
            "start_time": start_time,
            "end_time": end_time,
            "transfers": len(rows),
            "tokens": tokens,
            "api_calls": api_calls,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "warnings": [item for item in warnings if item],
        },
    }


def require_api_key() -> str:
    key = db.api_key()
    if not key:
        raise EtherscanError("Add an Etherscan API key in Settings first.")
    return key
