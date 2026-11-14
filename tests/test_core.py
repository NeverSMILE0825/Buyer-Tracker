import asyncio
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tracker.analyze import build_rows, decode_universal_router, _selector
from tracker.checksum import to_checksum_address
from tracker.db import Database
from tracker.cache import REORG_BLOCKS, collect_records, uncovered_spans
from tracker.etherscan import QueryTimeout, classify, fetch_records, parallel_walk, split_span, walk_records
from tracker.service import block_fraction
from tracker.formatutil import clean_label, format_amount, pretty_method, sort_units, token_amount
from tracker.labels import V4_CAPABLE_ROUTERS

WALLET = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
POOL = "0x" + "33" * 20
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
V2 = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
V3 = "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45"
V4 = "0x000000000004444c5dc75cb358380d2e3de08a90"
UR = "0x66a9893cc07d91d95644aedd05d03f95e1dba8af"
HASH = "0x" + "ab" * 32
HASH_B = "0x" + "cd" * 32


def transfer(**overrides):
    row = {
        "blockNumber": "100",
        "timeStamp": "1700000000",
        "hash": HASH,
        "from": OTHER,
        "to": WALLET,
        "value": "176720000",
        "contractAddress": USDT,
        "tokenName": "Tether USD",
        "tokenSymbol": "USDT",
        "tokenDecimal": "6",
        "transactionIndex": "1",
        "methodId": "0x38ed1739",
        "functionName": "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
        "statusRep": "1",
    }
    row.update(overrides)
    return row


def tx(hash_=HASH, value="0", to=V3, is_error="0", input_data="0x"):
    return {
        "hash": hash_,
        "from": WALLET,
        "to": to,
        "value": value,
        "isError": is_error,
        "input": input_data,
        "functionName": "exactInputSingle(tuple)",
        "methodId": "0x414bf389",
        "blockNumber": "100",
    }


def execute_calldata(commands: bytes) -> str:
    selector = _selector("execute(bytes,bytes[],uint256)")
    head = (96).to_bytes(32, "big") + (0).to_bytes(32, "big") + (1).to_bytes(32, "big")
    pad = bytes((32 - len(commands) % 32) % 32)
    body = len(commands).to_bytes(32, "big") + commands + pad
    return "0x" + selector + (head + body).hex()


class FormatTests(unittest.TestCase):
    def test_token_amount_is_exact(self):
        self.assertEqual(format_amount(token_amount("176720000", 6)), "176.72")
        self.assertEqual(format_amount(token_amount("1", 18)), "0.000000000000000001")
        self.assertEqual(format_amount(Decimal("1000.5")), "1,000.5")
        self.assertEqual(format_amount(Decimal("-1.5")), "-1.5")
        self.assertEqual(format_amount(Decimal("0")), "0")

    def test_sort_units_keep_order(self):
        self.assertLess(int(sort_units(Decimal("-2"))), int(sort_units(Decimal("-1"))))
        self.assertLess(int(sort_units(Decimal("1"))), int(sort_units(Decimal("1.5"))))

    def test_pretty_method(self):
        self.assertEqual(pretty_method("swapExactETHForTokens(uint256,address[],address,uint256)"), "Swap Exact ETH For Tokens")
        self.assertEqual(pretty_method("transferAndMulticall(address,uint256,bytes)"), "Transfer And Multicall")
        self.assertEqual(pretty_method("transfer(address,uint256)"), "Transfer")

    def test_label_length(self):
        self.assertEqual(clean_label("  desk  "), "desk")
        with self.assertRaises(ValueError):
            clean_label("x" * 81)


class ChecksumTests(unittest.TestCase):
    def test_eip55_vectors(self):
        vectors = [
            "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
            "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
            "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
            "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
        ]
        for vector in vectors:
            self.assertEqual(to_checksum_address(vector.lower()), vector)

    def test_rejects_short_address(self):
        with self.assertRaises(ValueError):
            to_checksum_address("0x1234")


class AnalyzeTests(unittest.TestCase):
    def test_eth_spent_and_token_in(self):
        rows, warnings = build_rows(
            WALLET,
            "Desk",
            {},
            [transfer()],
            [tx(value=str(10**18), to=V3)],
            [],
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["amount_token"], "176.72")
        self.assertEqual(row["amount_eth"], "-1")
        self.assertEqual(row["direction"], "in")
        self.assertEqual(row["token"], "Tether USD (USDT)")
        self.assertEqual(row["protocol"], "Uniswap V3")
        self.assertEqual(row["method"], "Swap Exact Tokens For Tokens")
        self.assertEqual(row["to_label"], "Desk")
        self.assertEqual(row["count"], 1)
        self.assertIn("uniswap v3", row["token"].lower() + row["protocol"].lower())

    def test_weth_and_internal_and_unrelated_internal(self):
        weth_out = transfer(
            from_=None,
            contractAddress=WETH,
            tokenName="Wrapped Ether",
            tokenSymbol="WETH",
            tokenDecimal="18",
            value=str(5 * 10**17),
            **{"from": WALLET, "to": POOL},
        )
        usdc_in = transfer(
            hash=HASH,
            contractAddress=USDC,
            tokenName="USD Coin",
            tokenSymbol="USDC",
            tokenDecimal="6",
            value="25000000",
            transactionIndex="2",
            **{"from": POOL, "to": WALLET},
        )
        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [weth_out, usdc_in],
            [tx(value="0", to=V2)],
            [
                {"hash": HASH, "from": V2, "to": WETH, "value": str(10**18), "isError": "0"},
                {"hash": HASH, "from": POOL, "to": WALLET, "value": str(2 * 10**17), "isError": "0"},
            ],
        )
        self.assertEqual({row["amount_eth"] for row in rows}, {"-0.3"})
        usdc = next(row for row in rows if row["token_symbol"] == "USDC")
        self.assertEqual(usdc["amount_token"], "25")
        self.assertEqual(usdc["protocol"], "Uniswap V2")
        self.assertEqual(usdc["count"], 1)

    def test_wrap_nets_to_zero_and_failed_tx_is_ignored(self):
        minted = transfer(
            contractAddress=WETH,
            tokenName="Wrapped Ether",
            tokenSymbol="WETH",
            tokenDecimal="18",
            value=str(10**18),
            **{"from": WETH, "to": WALLET},
        )
        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [minted],
            [tx(value=str(10**18), to=WETH, is_error="0")],
            [],
        )
        self.assertEqual(rows[0]["amount_eth"], "0")

        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [transfer()],
            [tx(value=str(10**18), to=V3, is_error="1")],
            [],
        )
        self.assertEqual(rows[0]["amount_eth"], "0")

    def test_count_is_distinct_transactions_per_token(self):
        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [
                transfer(hash=HASH, transactionIndex="1"),
                transfer(hash=HASH, transactionIndex="2", value="1"),
                transfer(hash=HASH_B, blockNumber="101", timeStamp="1700000012"),
                transfer(
                    hash=HASH_B,
                    blockNumber="101",
                    contractAddress=USDC,
                    tokenName="USD Coin",
                    tokenSymbol="USDC",
                    tokenDecimal="6",
                    value="1000000",
                ),
            ],
            [],
            [],
        )
        usdt = [row for row in rows if row["token_symbol"] == "USDT"]
        usdc = [row for row in rows if row["token_symbol"] == "USDC"]
        self.assertEqual({row["count"] for row in usdt}, {2})
        self.assertEqual(usdc[0]["count"], 1)
        self.assertEqual(len(usdt), 3)

    def test_user_label_overrides_known_and_v4_tag(self):
        labeled = to_checksum_address(V4)
        rows, _ = build_rows(
            WALLET,
            "",
            {labeled: "Main pool"},
            [transfer(**{"from": V4, "to": WALLET})],
            [],
            [],
        )
        self.assertEqual(rows[0]["from_label"], "Main pool")
        self.assertIn("Uniswap V4", rows[0]["protocol"])

    def test_universal_router_commands(self):
        self.assertEqual(_selector("execute(bytes,bytes[],uint256)"), "3593564c")
        self.assertIn(UR.lower(), V4_CAPABLE_ROUTERS)
        data = execute_calldata(bytes([0x00, 0x08, 0x90]))
        self.assertEqual(
            decode_universal_router(data, v4_capable=True),
            ["Uniswap V3", "Uniswap V2", "Uniswap V4"],
        )
        self.assertEqual(
            decode_universal_router(data, v4_capable=False),
            ["Uniswap V3", "Uniswap V2"],
        )
        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [transfer()],
            [tx(to=UR, input_data=data)],
            [],
        )
        self.assertEqual(rows[0]["protocol"], "Uniswap V2, Uniswap V3, Uniswap V4")

    def test_zero_and_unsafe(self):
        rows, _ = build_rows(
            WALLET,
            "",
            {},
            [transfer(value="0", statusRep="2")],
            [],
            [],
        )
        self.assertTrue(rows[0]["zero"])
        self.assertTrue(rows[0]["unsafe"])
        self.assertEqual(rows[0]["amount_token"], "0")


class WalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_window_keeps_boundary_block_once(self):
        blocks = [100, 101, 102, 103, 103, 104, 150]
        records = [{"blockNumber": str(block), "n": index} for index, block in enumerate(blocks)]

        async def fetch_page(start, end, page):
            matched = [row for row in records if start <= int(row["blockNumber"]) <= end]
            offset = (page - 1) * 2
            return matched[offset : offset + 2]

        rows, warnings = await walk_records(fetch_page, 100, 200, 2, 2)
        self.assertEqual([row["n"] for row in rows], list(range(7)))
        self.assertEqual(warnings, [])

    async def test_single_block_over_the_window_warns(self):
        records = [{"blockNumber": "5", "n": index} for index in range(5)]

        async def fetch_page(start, end, page):
            matched = [row for row in records if start <= int(row["blockNumber"]) <= end]
            offset = (page - 1) * 2
            return matched[offset : offset + 2]

        rows, warnings = await walk_records(fetch_page, 5, 5, 2, 2)
        self.assertEqual(len(rows), 4)
        self.assertTrue(warnings)
        self.assertIn("Block 5", warnings[0])

    async def test_timeout_splits_the_range(self):
        async def fetch_page(start, end, page):
            if page > 1:
                return []
            if end - start > 4:
                raise QueryTimeout("slow")
            return [{"blockNumber": str(start)}]

        rows, warnings = await fetch_records(fetch_page, 0, 10, 2, 2)
        self.assertEqual(sorted(int(row["blockNumber"]) for row in rows), [0, 3, 6])
        self.assertEqual(warnings, [])

    async def test_progress_ends_at_the_requested_block(self):
        seen = []

        async def fetch_page(start, end, page):
            if page > 1:
                return []
            return [{"blockNumber": "3"}, {"blockNumber": "8"}]

        async def on_progress(rows, covered):
            seen.append((rows, covered))

        rows, warnings = await walk_records(fetch_page, 1, 20, 100, 10, on_progress)
        self.assertEqual(len(rows), 2)
        self.assertEqual(warnings, [])
        self.assertEqual(seen[0], (2, 8))
        self.assertEqual(seen[-1], (2, 20))
        self.assertEqual([item[1] for item in seen], sorted(item[1] for item in seen))


class FractionTests(unittest.TestCase):
    def test_block_fraction_tracks_the_span(self):
        self.assertEqual(block_fraction(None, 10, 19), 0)
        self.assertEqual(block_fraction(9, 10, 19), 0)
        self.assertEqual(block_fraction(10, 10, 19), 0.1)
        self.assertEqual(block_fraction(19, 10, 19), 1)
        self.assertEqual(block_fraction(40, 10, 19), 1)
        self.assertEqual(block_fraction(7, 7, 7), 1)


class SplitTests(unittest.TestCase):
    def test_split_span_covers_without_overlap(self):
        spans = split_span(0, 9, 3)
        self.assertEqual(spans, [(0, 3), (4, 6), (7, 9)])
        self.assertEqual(split_span(5, 5, 3), [(5, 5)])
        covered = []
        for start, end in split_span(10, 20, 4):
            covered.extend(range(start, end + 1))
        self.assertEqual(covered, list(range(10, 21)))


class ParallelWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_lanes_return_every_row_once_and_overlap(self):
        records = [{"blockNumber": str(block), "n": block} for block in range(20)]
        in_flight = 0
        peak = 0

        async def fetch_page(start, end, page):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            matched = [row for row in records if start <= int(row["blockNumber"]) <= end]
            offset = (page - 1) * 2
            return matched[offset : offset + 2]

        rows, warnings = await parallel_walk(fetch_page, 0, 19, 2, 10, 3)
        self.assertEqual(sorted(row["n"] for row in rows), list(range(20)))
        self.assertEqual(len(rows), 20)
        self.assertEqual(warnings, [])
        self.assertGreaterEqual(peak, 2)

    async def test_single_block_over_the_window_warns(self):
        records = [{"blockNumber": "5", "n": index} for index in range(5)]

        async def fetch_page(start, end, page):
            matched = [row for row in records if start <= int(row["blockNumber"]) <= end]
            offset = (page - 1) * 2
            return matched[offset : offset + 2]

        rows, warnings = await parallel_walk(fetch_page, 5, 5, 2, 2, 3)
        self.assertEqual(len(rows), 4)
        self.assertTrue(warnings)
        self.assertIn("Block 5", warnings[0])


class ClientMessageTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("No transactions found", []), "empty")
        self.assertEqual(classify("NOTOK", "Max rate limit reached"), "rate")
        self.assertEqual(classify("NOTOK", "Free tier is limited to 1000 records"), "pagesize")
        self.assertEqual(classify("NOTOK", "Result window is too large"), "window")
        self.assertEqual(classify("NOTOK", "Invalid API Key"), "key")


class DatabaseTests(unittest.TestCase):
    def test_save_label_is_optional_and_checksummed(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "tracker.db")
            saved = database.save_wallet(WALLET, "")
            self.assertEqual(saved["address"], to_checksum_address(WALLET))
            self.assertEqual(saved["label"], "")
            self.assertEqual(database.label_map(), {})
            updated = database.save_wallet(WALLET.lower(), "Desk")
            self.assertEqual(updated["label"], "Desk")
            self.assertEqual(len(database.wallets()), 1)
            self.assertTrue(database.delete_wallet(saved["address"]))
            self.assertEqual(database.wallets(), [])
            database._conn.close()

    def test_notify_tokens_keep_a_label_when_the_symbol_is_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "tracker.db")
            contract = "0x" + "55" * 20
            saved = database.save_notify_token(contract, "USDC", "USD Coin", "Stable")
            self.assertEqual(saved["address"], to_checksum_address(contract))
            self.assertEqual(saved["label"], "Stable")
            refreshed = database.save_notify_token(contract.lower(), "USDC", "USD Coin")
            self.assertEqual(refreshed["label"], "Stable")
            self.assertEqual(len(database.notify_tokens()), 1)
            self.assertTrue(database.delete_notify_token(saved["address"]))
            self.assertEqual(database.notify_tokens(), [])
            with self.assertRaises(ValueError):
                database.save_notify_token("0x123", "", "", "")
            database._conn.close()

    def test_api_keys_can_be_enabled_together(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "tracker.db")
            first = database.save_api_key("FreeKey12345678", "")
            self.assertEqual(first["label"], "")
            self.assertTrue(first["enabled"])
            self.assertEqual(first["hint"], "Free…5678")
            self.assertNotIn("key", first)
            second = database.save_api_key("PaidKey12345678", "Paid plan")
            listed = database.api_keys()
            self.assertEqual(len(listed), 2)
            self.assertTrue(all(item["enabled"] for item in listed))
            self.assertNotIn("FreeKey12345678", json.dumps(listed))
            self.assertNotIn("PaidKey12345678", json.dumps(listed))
            self.assertEqual(
                set(database.enabled_api_keys()),
                {"FreeKey12345678", "PaidKey12345678"},
            )
            disabled = database.set_api_key_enabled(second["id"], False)
            self.assertIsNotNone(disabled)
            self.assertFalse(disabled["enabled"])
            self.assertEqual(database.enabled_api_keys(), ["FreeKey12345678"])
            self.assertIsNone(database.set_api_key_enabled(999, True))
            database.set_api_key_enabled(first["id"], False)
            self.assertEqual(database.api_key(), "")
            self.assertTrue(database.delete_api_key(first["id"]))
            with self.assertRaises(ValueError):
                database.save_api_key("short", "")
            database._conn.close()

    def test_saved_keys_stay_enabled_when_the_column_is_added(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tracker.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    key TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO api_keys (label, key, active, created_at, updated_at) VALUES ('', 'OldKey123456789', 1, 1, 1)"
            )
            conn.execute(
                "INSERT INTO api_keys (label, key, active, created_at, updated_at) VALUES ('Spare', 'SpareKey1234567', 0, 1, 1)"
            )
            conn.commit()
            conn.close()
            database = Database(path)
            self.assertEqual(
                set(database.enabled_api_keys()),
                {"OldKey123456789", "SpareKey1234567"},
            )
            database._conn.close()

    def test_legacy_settings_key_is_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tracker.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO settings (key, value) VALUES ('api_key', 'LegacyKey123456')")
            conn.commit()
            conn.close()
            database = Database(path)
            self.assertEqual(database.api_key(), "LegacyKey123456")
            self.assertEqual(database.get_setting("api_key", ""), "")
            listed = database.api_keys()
            self.assertEqual(len(listed), 1)
            self.assertNotIn("LegacyKey123456", json.dumps(listed))
            database._conn.close()


class ApiTests(unittest.TestCase):
    def test_wallet_and_track_without_key(self):
        from fastapi.testclient import TestClient

        from app import app
        from tracker.db import db

        address = to_checksum_address("0x" + "44" * 20)
        secret = "HttpTestKey123456"
        with TestClient(app) as client:
            created_id = None
            try:
                created = client.post("/api/wallets", json={"address": address, "label": ""})
                self.assertEqual(created.status_code, 200)
                self.assertEqual(created.json()["label"], "")
                listed = client.get("/api/wallets")
                self.assertTrue(any(item["address"] == created.json()["address"] for item in listed.json()))
                renamed = client.post("/api/wallets", json={"address": address, "label": "Cold"})
                self.assertEqual(renamed.json()["label"], "Cold")
                before = client.get("/api/keys")
                self.assertEqual(before.status_code, 200)
                saved = client.post("/api/keys", json={"key": secret, "label": "CI"})
                self.assertEqual(saved.status_code, 200)
                created_id = saved.json()["id"]
                self.assertEqual(saved.json()["label"], "CI")
                self.assertTrue(saved.json()["enabled"])
                self.assertNotIn(secret, saved.text)
                turned_off = client.post(
                    f"/api/keys/{created_id}/enabled",
                    json={"enabled": False},
                )
                self.assertEqual(turned_off.status_code, 200)
                self.assertFalse(turned_off.json()["enabled"])
                keys = client.get("/api/keys")
                self.assertNotIn(secret, keys.text)
                self.assertIn("API keys", client.get("/").text)
                removed = client.delete(f"/api/keys/{created_id}")
                self.assertEqual(removed.status_code, 200)
                created_id = None
                if not db.api_key():
                    missing = client.post("/api/track", json={
                        "address": address,
                        "mode": "block",
                        "start_block": 1,
                        "end_block": 2,
                    })
                    self.assertEqual(missing.status_code, 200)
                    event = json.loads(missing.text.strip().splitlines()[-1])
                    self.assertEqual(event["type"], "error")
                    self.assertIn("API key", event["message"])
            finally:
                db.delete_wallet(address)
                if created_id is not None:
                    db.delete_api_key(created_id)


class CacheTests(unittest.TestCase):
    def test_uncovered_spans_skip_saved_blocks_and_reopen_the_tip(self):
        self.assertEqual(uncovered_spans(0, 40, [], None), [(0, 40)])
        self.assertEqual(uncovered_spans(0, 40, [(0, 40)], None), [])
        self.assertEqual(uncovered_spans(0, 80, [(0, 50)], None), [(51, 80)])
        self.assertEqual(uncovered_spans(0, 40, [(0, 10), (30, 40)], None), [(11, 29)])
        head = 100
        tip = head - REORG_BLOCKS + 1
        self.assertEqual(uncovered_spans(0, head, [(0, head)], head), [(tip, head)])
        self.assertEqual(uncovered_spans(0, tip - 1, [(0, head)], head), [])

    def test_saved_rows_replace_a_refetched_block(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "tracker.db")
            wallet = "0x" + "ab" * 20
            first = {"blockNumber": "5", "hash": "0xaaa", "logIndex": "1", "value": "1"}
            database.replace_cached_rows(wallet, "tokentx", 1, 10, [first])
            database.add_coverage(wallet, "tokentx", 1, 10)
            self.assertEqual(database.coverage_spans(wallet, "tokentx"), [(1, 10)])
            replacement = {"blockNumber": "5", "hash": "0xbbb", "logIndex": "1", "value": "2"}
            database.replace_cached_rows(wallet, "tokentx", 5, 5, [replacement])
            loaded = database.load_cached_rows(wallet, "tokentx", 1, 10)
            self.assertEqual([row["hash"] for row in loaded], ["0xbbb"])
            database.add_coverage(wallet, "tokentx", 11, 12)
            self.assertEqual(database.coverage_spans(wallet, "tokentx"), [(1, 12)])
            database._conn.close()


class CollectTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_scan_fetches_only_the_new_tip(self):
        wallet = "0x" + "cd" * 20
        rows = [
            {"blockNumber": "10", "hash": "0x1", "logIndex": "1"},
            {"blockNumber": "20", "hash": "0x2", "logIndex": "1"},
        ]

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def fetch_account(self, kind, address, start, end, on_progress=None):
                self.calls.append((kind, start, end))
                matched = [row for row in rows if start <= int(row["blockNumber"]) <= end]
                if on_progress is not None:
                    await on_progress(len(matched), end - start + 1, end - start + 1)
                return matched, []

        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "tracker.db")
            client = FakeClient()
            loaded, warnings, cached, fetched = await collect_records(
                database, client, wallet, "tokentx", 10, 20, 1_000, None
            )
            self.assertEqual(warnings, [])
            self.assertEqual(cached, 0)
            self.assertEqual(fetched, 11)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(client.calls, [("tokentx", 10, 20)])

            client.calls.clear()
            loaded, _, cached, fetched = await collect_records(
                database, client, wallet, "tokentx", 10, 20, 1_000, None
            )
            self.assertEqual(client.calls, [])
            self.assertEqual(fetched, 0)
            self.assertEqual(cached, 11)
            self.assertEqual(len(loaded), 2)

            loaded, _, _, fetched = await collect_records(
                database, client, wallet, "tokentx", 10, 30, 1_000, None
            )
            self.assertEqual(client.calls, [("tokentx", 21, 30)])
            self.assertEqual(fetched, 10)
            self.assertEqual(len(loaded), 2)
            database._conn.close()


if __name__ == "__main__":
    unittest.main()
