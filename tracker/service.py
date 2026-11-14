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
Progress = Callable[[str, str], Awaitable[None]]


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
    wallet = normalize_address(address)
    saved = db.get_wallet(wallet)
    if saved is not None:
        wallet_label = saved["label"]
    else:
        wallet_label = clean_label(label)

    await progress("Resolving blocks", "")
    resolved = await resolve_range(client, mode, start_time, end_time, start_block, end_block)
    if resolved is None:
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

    async def token_progress(count: int) -> None:
        await progress("Fetching token transfers", f"{count:,} rows")

    async def tx_progress(count: int) -> None:
        await progress("Fetching ETH transactions", f"{count:,} rows")

    async def internal_progress(count: int) -> None:
        await progress("Fetching internal ETH", f"{count:,} rows")

    transfers, warnings = await client.fetch_account(
        "tokentx", wallet, start, end, token_progress
    )
    if not transfers:
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

    tx_task = client.fetch_account("txlist", wallet, span_start, span_end, tx_progress)
    internal_task = client.fetch_account(
        "txlistinternal", wallet, span_start, span_end, internal_progress
    )
    (transactions, tx_warnings), (internals, internal_warnings) = await asyncio.gather(
        tx_task, internal_task
    )
    warnings.extend(tx_warnings)
    warnings.extend(internal_warnings)

    await progress("Building the table", f"{len(transfers):,} transfers")
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
