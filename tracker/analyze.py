"""Join Etherscan tokentx, txlist, and internal transactions into table rows.

Amount (ETH) is the tracked wallet's net ETH plus net WETH for the whole
transaction. WETH is 1:1 with ETH. The top-level transaction value and internal
calls are added once each: Etherscan's internal list does not repeat tx.value.
Every token row from that transaction shows the same net, so the column is not
a per-row price and should not be summed down the table.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from Crypto.Hash import keccak

from tracker.checksum import to_checksum_address
from tracker.formatutil import exact_amount, format_amount, pretty_method, sort_units, token_amount, token_display
from tracker.labels import (
    COMMAND_MASK,
    DIRECT_PROTOCOL,
    KNOWN_LABELS,
    UNIVERSAL_ROUTERS,
    V2_COMMANDS,
    V3_COMMANDS,
    V4_CAPABLE_ROUTERS,
    V4_COMMAND,
    V4_PARTIES,
    WETH,
)

WEI = Decimal(10) ** 18
WETH_LOWER = WETH.lower()
_PROTOCOL_ORDER = ("Uniswap V2", "Uniswap V3", "Uniswap V4", "SushiSwap", "1inch", "0x", "Balancer", "Uniswap Universal Router")


def _selector(signature: str) -> str:
    digest = keccak.new(digest_bits=256)
    digest.update(signature.encode("ascii"))
    return digest.hexdigest()[:8]


EXECUTE_SELECTORS = {
    _selector("execute(bytes,bytes[],uint256)"),
    _selector("execute(bytes,bytes[])"),
}


def decode_universal_router(input_data: str, *, v4_capable: bool) -> list[str]:
    """Read Universal Router command bytes and return the pools they touch."""
    if not input_data or input_data == "deprecated":
        return []
    raw = input_data[2:] if input_data.startswith(("0x", "0X")) else input_data
    raw = raw.lower()
    if len(raw) < 8 + 64 or raw[:8] not in EXECUTE_SELECTORS:
        return []
    body = raw[8:]
    try:
        offset = int(body[:64], 16) * 2
        if offset < 0 or offset + 64 > len(body):
            return []
        length = int(body[offset : offset + 64], 16)
        start = offset + 64
        commands = body[start : start + length * 2]
        if len(commands) != length * 2:
            return []
    except ValueError:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for index in range(0, len(commands), 2):
        command = int(commands[index : index + 2], 16) & COMMAND_MASK
        name = ""
        if command in V3_COMMANDS:
            name = "Uniswap V3"
        elif command in V2_COMMANDS:
            name = "Uniswap V2"
        elif command == V4_COMMAND and v4_capable:
            name = "Uniswap V4"
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def protocols_for(tx_to: str, counterparties: set[str], input_data: str) -> list[str]:
    destination = (tx_to or "").lower()
    parties = {item.lower() for item in counterparties if item}
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            found.append(name)

    if destination in DIRECT_PROTOCOL:
        add(DIRECT_PROTOCOL[destination])
    if destination in UNIVERSAL_ROUTERS:
        versions = decode_universal_router(input_data, v4_capable=destination in V4_CAPABLE_ROUTERS)
        if versions:
            for version in versions:
                add(version)
        else:
            add("Uniswap Universal Router")
    if destination in V4_PARTIES or parties & V4_PARTIES:
        add("Uniswap V4")
    order = {name: index for index, name in enumerate(_PROTOCOL_ORDER)}
    found.sort(key=lambda name: order.get(name, len(order)))
    return found


def _label_for(address: str, wallet: str, wallet_label: str, user_labels: dict[str, str]) -> str:
    lowered = address.lower()
    if lowered == wallet.lower() and wallet_label:
        return wallet_label
    saved = user_labels.get(lowered) or user_labels.get(address)
    if saved:
        return saved
    # Checksum and lowercase keys both work; the book stores checksums.
    for key, value in user_labels.items():
        if key.lower() == lowered and value:
            return value
    return KNOWN_LABELS.get(lowered, "")


def _signed_native(wallet: str, tx: dict[str, Any] | None, internals: list[dict[str, Any]]) -> int:
    total = 0
    if tx and str(tx.get("isError", "0")) != "1":
        value = int(tx.get("value") or "0")
        if (tx.get("from") or "").lower() == wallet:
            total -= value
        if (tx.get("to") or "").lower() == wallet:
            total += value
    for item in internals:
        if str(item.get("isError", "0")) == "1":
            continue
        value = int(item.get("value") or "0")
        if value == 0:
            continue
        if (item.get("from") or "").lower() == wallet:
            total -= value
        if (item.get("to") or "").lower() == wallet:
            total += value
    return total


def _decimals(raw: Any) -> int | None:
    try:
        decimals = int(str(raw))
    except (TypeError, ValueError):
        return None
    if decimals < 0 or decimals > 255:
        return None
    return decimals


def build_rows(
    wallet: str,
    wallet_label: str,
    user_labels: dict[str, str],
    transfers: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    internals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    wallet_lower = wallet.lower()
    warnings: list[str] = []
    tx_by_hash: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        tx_hash = (tx.get("hash") or "").lower()
        if tx_hash and tx_hash not in tx_by_hash:
            tx_by_hash[tx_hash] = tx
    internal_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in internals:
        tx_hash = (item.get("hash") or "").lower()
        if tx_hash:
            internal_by_hash[tx_hash].append(item)

    prepared: list[dict[str, Any]] = []
    bad_decimals = 0
    for index, transfer in enumerate(transfers):
        decimals = _decimals(transfer.get("tokenDecimal"))
        if decimals is None:
            bad_decimals += 1
            decimals = 0
        try:
            amount = token_amount(str(transfer.get("value") or "0"), decimals)
        except (ValueError, ArithmeticError):
            bad_decimals += 1
            amount = Decimal(0)
        sender = transfer.get("from") or ""
        recipient = transfer.get("to") or ""
        sender_l = sender.lower()
        recipient_l = recipient.lower()
        if sender_l == wallet_lower and recipient_l == wallet_lower:
            direction = "self"
        elif recipient_l == wallet_lower:
            direction = "in"
        elif sender_l == wallet_lower:
            direction = "out"
        else:
            direction = "other"
        tx_hash = (transfer.get("hash") or "").lower()
        prepared.append(
            {
                "index": index,
                "hash": tx_hash,
                "block": int(transfer.get("blockNumber") or 0),
                "timestamp": int(transfer.get("timeStamp") or 0),
                "tx_index": int(transfer.get("transactionIndex") or 0),
                "from": sender,
                "to": recipient,
                "direction": direction,
                "amount": amount,
                "decimals": decimals,
                "contract": (transfer.get("contractAddress") or "").lower(),
                "token_name": transfer.get("tokenName") or "",
                "token_symbol": transfer.get("tokenSymbol") or "",
                "method_raw": (transfer.get("functionName") or "").strip(),
                "method_id": (transfer.get("methodId") or "").strip(),
                "unsafe": str(transfer.get("statusRep", "")) == "2",
            }
        )
    if bad_decimals:
        warnings.append(
            f"{bad_decimals:,} transfer{'s' if bad_decimals != 1 else ''} had an unreadable token decimal and the amount is shown as the raw integer."
        )

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in prepared:
        by_hash[item["hash"]].append(item)

    eth_by_hash: dict[str, Decimal] = {}
    protocol_by_hash: dict[str, str] = {}
    method_fallback: dict[str, tuple[str, str]] = {}
    for tx_hash, group in by_hash.items():
        tx = tx_by_hash.get(tx_hash)
        native = _signed_native(wallet_lower, tx, internal_by_hash.get(tx_hash, []))
        weth_net = Decimal(0)
        parties: set[str] = set()
        for item in group:
            parties.add(item["from"])
            parties.add(item["to"])
            if item["contract"] == WETH_LOWER:
                if item["direction"] == "in":
                    weth_net += item["amount"]
                elif item["direction"] == "out":
                    weth_net -= item["amount"]
                elif item["direction"] == "self":
                    pass
        eth_by_hash[tx_hash] = (Decimal(native) / WEI) + weth_net
        input_data = ""
        tx_to = ""
        if tx:
            input_data = tx.get("input") or ""
            tx_to = tx.get("to") or ""
            method_fallback[tx_hash] = (
                (tx.get("functionName") or "").strip(),
                (tx.get("methodId") or "").strip(),
            )
        names = protocols_for(tx_to, parties, input_data)
        protocol_by_hash[tx_hash] = ", ".join(names)

    counts: dict[str, set[str]] = defaultdict(set)
    for item in prepared:
        if item["hash"]:
            counts[item["contract"]].add(item["hash"])

    prepared.sort(key=lambda item: (item["block"], item["tx_index"], item["index"]))
    rows: list[dict[str, Any]] = []
    for seq, item in enumerate(prepared):
        method_raw = item["method_raw"]
        method_id = item["method_id"]
        if not method_raw and item["hash"] in method_fallback:
            method_raw, fallback_id = method_fallback[item["hash"]]
            method_id = method_id or fallback_id
        if method_raw:
            method = pretty_method(method_raw) or method_raw
        elif method_id and method_id not in {"0x", "0x0"}:
            method = method_id
        else:
            method = "Unknown"
        amount = item["amount"]
        eth = eth_by_hash.get(item["hash"], Decimal(0))
        contract = item["contract"]
        display_from = display_address(item["from"])
        display_to = display_address(item["to"])
        display_contract = display_address(contract)
        rows.append(
            {
                "seq": seq,
                "wallet": wallet,
                "wallet_label": wallet_label or "",
                "hash": item["hash"],
                "method": method,
                "method_raw": method_raw,
                "protocol": protocol_by_hash.get(item["hash"], ""),
                "block": item["block"],
                "timestamp": item["timestamp"],
                "from": display_from,
                "from_label": _label_for(item["from"], wallet, wallet_label, user_labels),
                "to": display_to,
                "to_label": _label_for(item["to"], wallet, wallet_label, user_labels),
                "direction": item["direction"],
                "amount_token": format_amount(amount),
                "amount_token_exact": exact_amount(amount),
                "amount_token_sort": sort_units(amount),
                "amount_eth": format_amount(eth),
                "amount_eth_exact": exact_amount(eth),
                "amount_eth_sort": sort_units(eth),
                "token_name": item["token_name"],
                "token_symbol": item["token_symbol"],
                "token": token_display(item["token_name"], item["token_symbol"]),
                "token_contract": display_contract,
                "count": len(counts.get(contract, ())),
                "zero": amount == 0,
                "unsafe": item["unsafe"],
            }
        )
    return rows, warnings


def display_address(address: str) -> str:
    try:
        return to_checksum_address(address)
    except ValueError:
        return address or ""
