"""Etherscan API v2 client.

Free-tier limits (July 2026): 3 calls/second and 1,000 records per request.
Account history is also capped at a 10,000-record window (page x offset), so a
full result is walked block by block instead of asking for page 11.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import httpx

from tracker.formatutil import mask_secret

API_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = "1"
RESULT_WINDOW = 10_000


class EtherscanError(Exception):
    pass


class QueryTimeout(EtherscanError):
    pass


class WindowExceeded(EtherscanError):
    pass


class PageSizeRejected(EtherscanError):
    """The plan refused this page size. Retry at the free-tier size of 1,000."""


def classify(message: str, result: Any) -> str:
    text = f"{message} {result if isinstance(result, str) else ''}".lower()
    if "no transaction" in text or "no record" in text or "no internal" in text:
        return "empty"
    if "rate limit" in text or "rate exceeded" in text:
        return "rate"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(
        hint in text
        for hint in (
            "free tier",
            "free api",
            "maximum of 1000",
            "max of 1000",
            "limited to 1000",
            "1000 records",
            "exceeds the limit of 1000",
        )
    ):
        return "pagesize"
    if (
        "result window" in text
        or "pageno" in text
        or "page no" in text
        or ("offset" in text and "less than or equal" in text)
    ):
        return "window"
    if "invalid api key" in text or "missing/invalid" in text or "missing or invalid api" in text:
        return "key"
    return "error"


class EtherscanClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient, rate: float, page_size: int):
        self.api_key = api_key.strip()
        self.http = http
        self.rate = min(30.0, max(1.0, float(rate)))
        self.page_size = min(10_000, max(100, int(page_size)))
        self.calls = 0
        self._interval = 1.0 / self.rate
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = now + self._interval

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        query = {"chainid": CHAIN_ID, "apikey": self.api_key, **params}
        last_text = "Etherscan request failed"
        for attempt in range(6):
            await self._pace()
            self.calls += 1
            try:
                response = await self.http.get(API_URL, params=query)
                try:
                    data = response.json()
                except ValueError as exc:
                    last_text = f"Etherscan returned a non-JSON response ({response.status_code})."
                    if attempt < 5 and response.status_code >= 500:
                        await asyncio.sleep(0.4 * (attempt + 1))
                        continue
                    raise EtherscanError(last_text) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_text = "Network error talking to Etherscan."
                if attempt == 5:
                    raise EtherscanError(last_text) from exc
                await asyncio.sleep(0.4 * (attempt + 1))
                continue

            if isinstance(data, dict) and "jsonrpc" in data:
                if data.get("error"):
                    message = data["error"].get("message", "Ethereum RPC error")
                    raise EtherscanError(self._scrub(str(message)))
                return data

            message = str(data.get("message", ""))
            result = data.get("result")
            kind = classify(message, result)
            detail = result if isinstance(result, str) and result else message
            last_text = self._scrub(str(detail or "Etherscan request failed"))
            if kind == "rate" or response.status_code == 429:
                await asyncio.sleep(0.35 * (attempt + 1))
                continue
            if kind == "timeout":
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise QueryTimeout(last_text)
            if kind == "pagesize":
                raise PageSizeRejected(last_text)
            if kind == "window":
                raise WindowExceeded(last_text)
            if kind == "key":
                raise EtherscanError("Etherscan rejected the API key. Check it in Settings.")
            if kind == "empty":
                data = {**data, "result": []}
                return data
            if str(data.get("status", "")) != "1":
                raise EtherscanError(last_text or "Etherscan request failed")
            return data
        raise EtherscanError(
            "Etherscan rate limit did not recover. Lower calls per second in Settings and try again."
        )

    def _scrub(self, message: str) -> str:
        if self.api_key and self.api_key in message:
            return message.replace(self.api_key, "…")
        return message

    async def call(self, params: dict[str, Any], *, as_list: bool = False) -> Any:
        data = await self._request(params)
        if "jsonrpc" in data:
            return data.get("result")
        result = data.get("result")
        if as_list:
            return result if isinstance(result, list) else []
        return result

    async def latest_block(self) -> int:
        result = await self.call({"module": "proxy", "action": "eth_blockNumber"})
        return int(str(result), 16)

    async def block_by_time(self, timestamp: int, closest: str) -> int:
        try:
            result = await self.call(
                {
                    "module": "block",
                    "action": "getblocknobytime",
                    "timestamp": str(int(timestamp)),
                    "closest": closest,
                }
            )
            return int(result)
        except EtherscanError as exc:
            # A future end time has no block after it; the latest block is the right edge.
            if closest == "before" and timestamp >= int(time.time()) - 30:
                return await self.latest_block()
            raise exc

    async def fetch_account(
        self,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        warnings: list[str] = []

        async def run(page_size: int) -> tuple[list[dict[str, Any]], list[str]]:
            window_pages = max(1, min(10, RESULT_WINDOW // page_size))

            async def fetch_page(start: int, end: int, page: int) -> list[dict[str, Any]]:
                rows = await self.call(
                    {
                        "module": "account",
                        "action": action,
                        "address": address,
                        "startblock": start,
                        "endblock": end,
                        "page": page,
                        "offset": page_size,
                        "sort": "asc",
                    },
                    as_list=True,
                )
                return rows

            return await fetch_records(
                fetch_page,
                start_block,
                end_block,
                page_size,
                window_pages,
                on_progress,
            )

        try:
            rows, walk_warnings = await run(self.page_size)
        except PageSizeRejected:
            if self.page_size <= 1000:
                raise
            self.page_size = 1000
            warnings.append(
                "This API key rejected the larger page size, so the scan continued at 1,000 records per request. Set Records per request to 1,000 in Settings."
            )
            rows, walk_warnings = await run(1000)
        warnings.extend(walk_warnings)
        return rows, warnings


async def walk_records(
    fetch_page: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    start: int,
    end: int,
    page_size: int,
    window_pages: int,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every record in [start, end] despite the 10,000-row result window.

    Pages are sorted ascending. When a window fills up, rows from its last block
    are discarded and that block is requested again, so a block split across the
    page boundary is not truncated and is not duplicated.

    on_progress receives the row count and the highest block fully covered.
    A short page means Etherscan has no further rows, so the covered block is `end`.
    """
    collected: list[dict[str, Any]] = []
    warnings: list[str] = []
    cursor = start

    async def report(rows: int, covered: int) -> None:
        if on_progress:
            await on_progress(rows, covered)

    while cursor <= end:
        window: list[dict[str, Any]] = []
        hit_end = False
        for page in range(1, window_pages + 1):
            try:
                batch = await fetch_page(cursor, end, page)
            except WindowExceeded:
                break
            window.extend(batch)
            if window:
                highest = max(int(row["blockNumber"]) for row in window)
                # A full page can split its last block onto the next page.
                covered = highest if len(batch) < page_size else highest - 1
                await report(len(collected) + len(window), covered)
            if len(batch) < page_size:
                hit_end = True
                break
        if not window and not hit_end:
            raise WindowExceeded(f"No records returned before the page window for blocks {cursor}-{end}.")
        if hit_end:
            collected.extend(window)
            await report(len(collected), end)
            break
        last_block = max(int(row["blockNumber"]) for row in window)
        if last_block <= cursor:
            collected.extend(window)
            cap = page_size * window_pages
            warnings.append(
                f"Block {cursor:,} has more than {cap:,} records, above Etherscan's page window. That block may be incomplete."
            )
            cursor = last_block + 1
            await report(len(collected), cursor - 1)
            continue
        collected.extend(row for row in window if int(row["blockNumber"]) < last_block)
        cursor = last_block
        await report(len(collected), cursor - 1)
    return collected, warnings


async def fetch_records(
    fetch_page: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    start: int,
    end: int,
    page_size: int,
    window_pages: int,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        return await walk_records(fetch_page, start, end, page_size, window_pages, on_progress)
    except (QueryTimeout, WindowExceeded) as exc:
        if start >= end or depth > 40:
            return [], [
                f"Block {start:,} could not be loaded from Etherscan ({exc}). Results are missing that block."
            ]
        mid = (start + end) // 2
        left_rows, left_warnings = await fetch_records(
            fetch_page, start, mid, page_size, window_pages, on_progress, depth + 1
        )
        right_rows, right_warnings = await fetch_records(
            fetch_page, mid + 1, end, page_size, window_pages, on_progress, depth + 1
        )
        return left_rows + right_rows, left_warnings + right_warnings


def split_span(start: int, end: int, parts: int) -> list[tuple[int, int]]:
    """Split the inclusive span [start, end] into contiguous, non-overlapping pieces."""
    if end < start:
        return []
    width = end - start + 1
    parts = max(1, min(int(parts), width))
    base, extra = divmod(width, parts)
    spans: list[tuple[int, int]] = []
    cursor = start
    for index in range(parts):
        size = base + (1 if index < extra else 0)
        spans.append((cursor, cursor + size - 1))
        cursor += size
    return spans


async def scan_span(
    fetch_page: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    start: int,
    end: int,
    page_size: int,
    window_pages: int,
    lanes: int,
    depth: int = 0,
    on_touch: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[int, int]], bool]:
    """Read one slice of [start, end].

    A full page that covers more than the cursor block is handed back as
    follow-up spans, so other keys can read the tail at the same time.
    The returned rows never include a block that a follow-up span will read.
    The bool is true when the follow-ups are a retry after a timeout.
    """
    collected: list[dict[str, Any]] = []
    warnings: list[str] = []
    cursor = start

    async def touch(rows: int, covered: int) -> None:
        if on_touch:
            await on_touch(rows, covered)

    while cursor <= end:
        window: list[dict[str, Any]] = []
        hit_end = False
        for page in range(1, window_pages + 1):
            try:
                batch = await fetch_page(cursor, end, page)
            except (QueryTimeout, WindowExceeded) as exc:
                if start >= end or depth > 40:
                    return [], [
                        f"Block {start:,} could not be loaded from Etherscan ({exc}). Results are missing that block."
                    ], [], False
                mid = (start + end) // 2
                return [], [], [(start, mid), (mid + 1, end)], True
            window.extend(batch)
            if window:
                highest = max(int(row["blockNumber"]) for row in window)
                covered = highest if len(batch) < page_size else highest - 1
                await touch(len(collected) + len(window), covered)
            if len(batch) < page_size:
                hit_end = True
                break
            highest = max(int(row["blockNumber"]) for row in window)
            if lanes > 1 and highest > cursor:
                kept = [row for row in window if int(row["blockNumber"]) < highest]
                collected.extend(kept)
                return collected, warnings, split_span(highest, end, lanes), False
        if hit_end:
            collected.extend(window)
            await touch(len(collected), end)
            return collected, warnings, [], False
        if not window:
            if cursor < end and depth <= 40:
                mid = (cursor + end) // 2
                return [], [], [(cursor, mid), (mid + 1, end)], True
            return [], [
                f"Block {cursor:,} could not be loaded from Etherscan. Results are missing that block."
            ], [], False
        last_block = max(int(row["blockNumber"]) for row in window)
        if last_block <= cursor:
            collected.extend(window)
            cap = page_size * window_pages
            warnings.append(
                f"Block {cursor:,} has more than {cap:,} records, above Etherscan's page window. That block may be incomplete."
            )
            cursor = last_block + 1
            await touch(len(collected), cursor - 1)
            continue
        if lanes > 1:
            kept = [row for row in window if int(row["blockNumber"]) < last_block]
            collected.extend(kept)
            return collected, warnings, split_span(last_block, end, lanes), False
        collected.extend(row for row in window if int(row["blockNumber"]) < last_block)
        cursor = last_block
        await touch(len(collected), cursor - 1)
    return collected, warnings, [], False


async def parallel_walk(
    fetch_page: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    start: int,
    end: int,
    page_size: int,
    window_pages: int,
    lanes: int,
    on_progress: Callable[[int, int, int], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Walk [start, end] with several lanes. Each lane uses its own fetch_page."""
    if end < start:
        return [], []
    lanes = max(1, lanes)
    total_blocks = end - start + 1
    collected: list[dict[str, Any]] = []
    warnings: list[str] = []
    done_blocks = 0
    done_rows = 0
    partial: dict[tuple[int, int], tuple[int, int]] = {}
    pending = 0
    jobs = 0
    state = asyncio.Lock()
    queue: asyncio.Queue[tuple[int, int, int] | None] = asyncio.Queue()

    async def emit() -> None:
        if on_progress is None:
            return
        blocks = done_blocks
        rows = done_rows
        for (span_start, span_end), (rows_here, covered) in partial.items():
            rows += rows_here
            if covered >= span_start:
                blocks += min(span_end, covered) - span_start + 1
        await on_progress(rows, min(total_blocks, max(0, blocks)), total_blocks)

    async def enqueue(span_start: int, span_end: int, depth: int) -> None:
        nonlocal pending, jobs
        if span_start > span_end:
            return
        async with state:
            jobs += 1
            if jobs > 200_000:
                raise EtherscanError("This range produced too many scan slices.")
            pending += 1
        await queue.put((span_start, span_end, depth))

    async def finish_job() -> None:
        nonlocal pending
        async with state:
            pending -= 1
            finished = pending == 0
        if finished:
            for _ in range(workers):
                await queue.put(None)

    async def worker(
        lane_fetch: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        nonlocal done_blocks, done_rows
        while True:
            item = await queue.get()
            if item is None:
                return
            span_start, span_end, depth = item
            try:
                async def on_touch(rows: int, covered: int) -> None:
                    nonlocal done_blocks, done_rows
                    async with state:
                        partial[(span_start, span_end)] = (rows, covered)
                        await emit()

                rows, span_warnings, followups, bisect = await scan_span(
                    lane_fetch,
                    span_start,
                    span_end,
                    page_size,
                    window_pages,
                    lanes,
                    depth,
                    on_touch,
                )
                async with state:
                    partial.pop((span_start, span_end), None)
                    collected.extend(rows)
                    warnings.extend(span_warnings)
                    if followups:
                        if rows:
                            boundary = followups[0][0]
                            if boundary > span_start:
                                done_blocks += boundary - span_start
                            done_rows += len(rows)
                    else:
                        done_blocks += span_end - span_start + 1
                        done_rows += len(rows)
                    await emit()
                next_depth = depth + 1 if bisect else 0
                for piece_start, piece_end in followups:
                    await enqueue(piece_start, piece_end, next_depth)
            finally:
                await finish_job()

    await enqueue(start, end, 0)
    lane_fetches = fetch_page if isinstance(fetch_page, list) else [fetch_page] * lanes
    workers = len(lane_fetches)
    try:
        async with asyncio.TaskGroup() as group:
            for lane_fetch in lane_fetches:
                group.create_task(worker(lane_fetch))
    except BaseExceptionGroup as group:
        errors = [exc for exc in group.exceptions if not isinstance(exc, asyncio.CancelledError)]
        if not errors:
            raise
        for exc in errors:
            if isinstance(exc, EtherscanError):
                raise exc
        raise errors[0]
    return collected, warnings


class KeyPool:
    """One paced client per enabled key. Account scans share the work across them."""

    def __init__(self, api_keys: list[str], http: httpx.AsyncClient, rate: float, page_size: int):
        if not api_keys:
            raise EtherscanError("Add an Etherscan API key in the sidebar first.")
        self.clients = [EtherscanClient(key, http, rate, page_size) for key in api_keys]
        self.page_size = min(10_000, max(100, int(page_size)))
        self._rr = 0
        self._rr_lock = asyncio.Lock()

    @property
    def calls(self) -> int:
        return sum(client.calls for client in self.clients)

    @property
    def key_count(self) -> int:
        return len(self.clients)

    async def next_client(self) -> EtherscanClient:
        async with self._rr_lock:
            client = self.clients[self._rr % len(self.clients)]
            self._rr += 1
            return client

    async def latest_block(self) -> int:
        client = await self.next_client()
        return await client.latest_block()

    async def block_by_time(self, timestamp: int, closest: str) -> int:
        client = await self.next_client()
        return await client.block_by_time(timestamp, closest)

    async def fetch_account(
        self,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        on_progress: Callable[[int, int, int], Awaitable[None]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if len(self.clients) == 1:
            return await self._fetch_one(self.clients[0], action, address, start_block, end_block, on_progress)
        warnings: list[str] = []
        try:
            rows, walk_warnings = await self._fetch_many(
                action, address, start_block, end_block, self.page_size, on_progress
            )
        except PageSizeRejected:
            if self.page_size <= 1000:
                raise
            self.page_size = 1000
            for client in self.clients:
                client.page_size = 1000
            warnings.append(
                "These API keys rejected the larger page size, so the scan continued at 1,000 records per request. Set Records per request to 1,000 in Settings."
            )
            rows, walk_warnings = await self._fetch_many(
                action, address, start_block, end_block, 1000, on_progress
            )
        warnings.extend(walk_warnings)
        return rows, warnings

    async def _fetch_one(
        self,
        client: EtherscanClient,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        on_progress: Callable[[int, int, int], Awaitable[None]] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        total = end_block - start_block + 1

        async def adapt(rows: int, reached: int) -> None:
            if on_progress is None:
                return
            if total <= 0:
                await on_progress(rows, 1, 1)
                return
            done = 0 if reached < start_block else min(end_block, reached) - start_block + 1
            await on_progress(rows, done, total)

        try:
            return await client.fetch_account(action, address, start_block, end_block, adapt)
        except EtherscanError as exc:
            raise self._keyed(client, exc) from exc

    async def _fetch_many(
        self,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        page_size: int,
        on_progress: Callable[[int, int, int], Awaitable[None]] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        window_pages = max(1, min(10, RESULT_WINDOW // page_size))

        def lane(client: EtherscanClient):
            async def fetch_page(start: int, end: int, page: int) -> list[dict[str, Any]]:
                try:
                    rows = await client.call(
                        {
                            "module": "account",
                            "action": action,
                            "address": address,
                            "startblock": start,
                            "endblock": end,
                            "page": page,
                            "offset": page_size,
                            "sort": "asc",
                        },
                        as_list=True,
                    )
                    return rows
                except EtherscanError as exc:
                    raise self._keyed(client, exc) from exc

            return fetch_page

        return await parallel_walk(
            [lane(client) for client in self.clients],
            start_block,
            end_block,
            page_size,
            window_pages,
            len(self.clients),
            on_progress,
        )

    @staticmethod
    def _keyed(client: EtherscanClient, exc: EtherscanError) -> EtherscanError:
        if "rejected the API key" not in str(exc):
            return exc
        return EtherscanError(
            f"Etherscan rejected {mask_secret(client.api_key)}. Turn that key off, or replace it, and try again."
        )
