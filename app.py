"""Local Buyer Tracker server. Binds to localhost and keeps the API key on disk."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tracker.db import db
from tracker.etherscan import EtherscanClient, EtherscanError
from tracker.service import require_api_key, track

log = logging.getLogger("buyertracker")
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


class NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(45.0, connect=10.0),
        headers={"Accept": "application/json", "User-Agent": "BuyerTracker/1.0"},
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=8),
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="Buyer Tracker", lifespan=lifespan)
app.mount("/static", NoCacheStatic(directory=STATIC), name="static")


class WalletIn(BaseModel):
    address: str
    label: str = ""


class ApiKeyIn(BaseModel):
    key: str
    label: str = ""


class SettingsIn(BaseModel):
    api_key: str | None = None
    clear_key: bool = False
    rate: float | None = None
    page_size: int | None = None


class TrackIn(BaseModel):
    address: str
    label: str = ""
    mode: Literal["time", "block"]
    start_time: int | None = None
    end_time: int | None = None
    start_block: int | None = Field(default=None, ge=0)
    end_block: int | None = Field(default=None, ge=0)


def _seconds(value: int | None) -> int | None:
    if value is None:
        return None
    while value > 10**11:
        value //= 1000
    return value


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(EtherscanError)
async def etherscan_error(_: Request, exc: EtherscanError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/wallets")
def list_wallets() -> list[dict[str, str]]:
    return db.wallets()


@app.post("/api/wallets")
def save_wallet(body: WalletIn) -> dict[str, str]:
    return db.save_wallet(body.address, body.label)


@app.delete("/api/wallets/{address}")
def delete_wallet(address: str) -> dict[str, bool]:
    if not db.delete_wallet(address):
        raise HTTPException(status_code=404, detail="Wallet is not saved.")
    return {"ok": True}


@app.get("/api/keys")
def list_keys() -> list[dict[str, Any]]:
    return db.api_keys()


@app.post("/api/keys")
def save_key(body: ApiKeyIn) -> dict[str, Any]:
    return db.save_api_key(body.key, body.label)


@app.post("/api/keys/{key_id}/activate")
def activate_key(key_id: int) -> dict[str, Any]:
    saved = db.activate_api_key(key_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="API key is not saved.")
    return saved


@app.delete("/api/keys/{key_id}")
def delete_key(key_id: int) -> dict[str, bool]:
    if not db.delete_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key is not saved.")
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {
        "has_key": bool(db.api_key()),
        "key_hint": db.key_hint(),
        "rate": db.rate(),
        "page_size": db.page_size(),
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict[str, Any]:
    if body.clear_key:
        db.clear_active_api_key()
    elif body.api_key is not None and body.api_key.strip():
        db.save_api_key(body.api_key.strip(), "")
    if body.rate is not None:
        if not 1 <= body.rate <= 30:
            raise ValueError("Calls per second must be between 1 and 30.")
        db.set_setting("rate", str(body.rate))
    if body.page_size is not None:
        if not 100 <= body.page_size <= 10_000:
            raise ValueError("Records per request must be between 100 and 10,000.")
        db.set_setting("page_size", str(body.page_size))
    return get_settings()


@app.get("/api/latest-block")
async def latest_block(request: Request) -> dict[str, int]:
    client = _client(request)
    block = await client.latest_block()
    return {"block": block}


@app.post("/api/track")
async def track_route(body: TrackIn, request: Request) -> StreamingResponse:
    async def generate():
        queue: asyncio.Queue = asyncio.Queue()

        async def progress(message: str, fraction: float = 0, extra: dict | None = None) -> None:
            event: dict[str, Any] = {
                "type": "progress",
                "message": message,
                "fraction": fraction,
            }
            if extra:
                event.update(extra)
            await queue.put(event)

        async def run() -> None:
            try:
                client = _client(request)
                payload = await track(
                    client,
                    body.address,
                    body.label,
                    body.mode,
                    _seconds(body.start_time),
                    _seconds(body.end_time),
                    body.start_block,
                    body.end_block,
                    progress,
                )
                await queue.put({"type": "done", **payload})
            except asyncio.CancelledError:
                raise
            except (ValueError, EtherscanError) as exc:
                await queue.put({"type": "error", "message": str(exc)})
            except Exception:
                log.exception("track failed")
                await queue.put(
                    {"type": "error", "message": "Something went wrong while loading transfers."}
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                yield json.dumps(item, separators=(",", ":")) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _client(request: Request) -> EtherscanClient:
    return EtherscanClient(require_api_key(), request.app.state.http, db.rate(), db.page_size())


def _free_port(start: int = 8787, attempts: int = 20) -> int:
    import socket

    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free localhost port between {start} and {start + attempts - 1}.")


if __name__ == "__main__":
    import uvicorn

    port = _free_port()
    print("Buyer Tracker")
    print(f"Open http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
