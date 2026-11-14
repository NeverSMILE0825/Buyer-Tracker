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
        on_progress: Callable[[int], Awaitable[None]] | None = None,
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
    on_progress: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every record in [start, end] despite the 10,000-row result window.

    Pages are sorted ascending. When a window fills up, rows from its last block
    are discarded and that block is requested again, so a block split across the
    page boundary is not truncated and is not duplicated.
    """
    collected: list[dict[str, Any]] = []
    warnings: list[str] = []
    cursor = start
    while cursor <= end:
        window: list[dict[str, Any]] = []
        hit_end = False
        for page in range(1, window_pages + 1):
            try:
                batch = await fetch_page(cursor, end, page)
            except WindowExceeded:
                break
            window.extend(batch)
            if on_progress:
                await on_progress(len(collected) + len(window))
            if len(batch) < page_size:
                hit_end = True
                break
        if not window and not hit_end:
            raise WindowExceeded(f"No records returned before the page window for blocks {cursor}-{end}.")
        if hit_end:
            collected.extend(window)
            break
        last_block = max(int(row["blockNumber"]) for row in window)
        if last_block <= cursor:
            collected.extend(window)
            cap = page_size * window_pages
            warnings.append(
                f"Block {cursor:,} has more than {cap:,} records, above Etherscan's page window. That block may be incomplete."
            )
            cursor = last_block + 1
            continue
        collected.extend(row for row in window if int(row["blockNumber"]) < last_block)
        cursor = last_block
    return collected, warnings


async def fetch_records(
    fetch_page: Callable[[int, int, int], Awaitable[list[dict[str, Any]]]],
    start: int,
    end: int,
    page_size: int,
    window_pages: int,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
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
