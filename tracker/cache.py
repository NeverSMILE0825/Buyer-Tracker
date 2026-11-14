"""Remember scans so a later fetch only asks Etherscan for the missing blocks.

The newest blocks are always read again. A short reorg can still change them.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

REORG_BLOCKS = 64
ProgressFn = Callable[[int, int, int], Awaitable[None]]


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(start), int(end)) for start, end in spans if start <= end)
    if not ordered:
        return []
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def uncovered_spans(
    start: int,
    end: int,
    covered: list[tuple[int, int]],
    head: int | None,
) -> list[tuple[int, int]]:
    """Blocks in [start, end] that are not saved, plus the reorg tail at the chain head."""
    if end < start:
        return []
    stable: list[tuple[int, int]] = []
    reopen = None if head is None else head - REORG_BLOCKS + 1
    for span_start, span_end in covered:
        if span_start > span_end:
            continue
        if reopen is not None and span_end >= reopen:
            span_end = reopen - 1
        if span_start <= span_end:
            stable.append((span_start, span_end))
    cursor = start
    gaps: list[tuple[int, int]] = []
    for span_start, span_end in merge_spans(stable):
        if span_end < cursor:
            continue
        if span_start > end:
            break
        if span_start > cursor:
            gaps.append((cursor, min(end, span_start - 1)))
        cursor = max(cursor, span_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def row_identity(kind: str, row: dict[str, Any]) -> tuple[int, str]:
    block = int(row.get("blockNumber") or 0)
    tx_hash = str(row.get("hash") or "").lower()
    if kind == "txlist":
        return block, tx_hash
    if kind == "tokentx":
        log_index = str(row.get("logIndex") or "")
        if log_index:
            return block, f"{tx_hash}:{log_index}"
        key = "|".join(
            (
                tx_hash,
                str(row.get("contractAddress") or "").lower(),
                str(row.get("from") or "").lower(),
                str(row.get("to") or "").lower(),
                str(row.get("value") or ""),
                str(row.get("transactionIndex") or ""),
            )
        )
        return block, key
    key = "|".join(
        (
            tx_hash,
            str(row.get("traceId") or ""),
            str(row.get("type") or ""),
            str(row.get("from") or "").lower(),
            str(row.get("to") or "").lower(),
            str(row.get("value") or ""),
        )
    )
    return block, key


async def collect_records(
    database: Any,
    client: Any,
    wallet: str,
    kind: str,
    start: int,
    end: int,
    head: int | None,
    on_progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Return saved rows for [start, end], fetching only the spans still missing."""
    covered = database.coverage_spans(wallet, kind)
    gaps = uncovered_spans(start, end, covered, head)
    warnings: list[str] = []
    width = max(0, end - start + 1)
    fetched_blocks = sum(gap_end - gap_start + 1 for gap_start, gap_end in gaps)
    cached_blocks = max(0, width - fetched_blocks)
    done_blocks = cached_blocks
    if on_progress is not None:
        await on_progress(0, done_blocks, width)

    for gap_start, gap_end in gaps:
        gap_width = gap_end - gap_start + 1
        base = done_blocks

        async def gap_progress(
            rows: int,
            blocks_done: int,
            blocks_total: int,
            base: int = base,
            gap_width: int = gap_width,
        ) -> None:
            if on_progress is None:
                return
            portion = 0 if not blocks_total else blocks_done / blocks_total
            await on_progress(rows, base + int(portion * gap_width), width)

        rows, gap_warnings = await client.fetch_account(
            kind, wallet, gap_start, gap_end, gap_progress
        )
        database.replace_cached_rows(wallet, kind, gap_start, gap_end, rows)
        database.add_coverage(wallet, kind, gap_start, gap_end)
        warnings.extend(gap_warnings)
        done_blocks += gap_width

    records = database.load_cached_rows(wallet, kind, start, end)
    if on_progress is not None:
        await on_progress(len(records), width, width)
    return records, warnings, cached_blocks, fetched_blocks
