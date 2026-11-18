"""Exact decimal formatting. Token amounts never pass through binary floats."""

import re
from decimal import Decimal, ROUND_HALF_UP

WEI = Decimal(10) ** 18
SORT_SCALE = Decimal(10) ** 36
_ACRONYMS = {
    "eth": "ETH",
    "weth": "WETH",
    "btc": "BTC",
    "wbtc": "WBTC",
    "nft": "NFT",
    "api": "API",
    "usd": "USD",
    "usdt": "USDT",
    "usdc": "USDC",
    "dai": "DAI",
}


def token_amount(raw_value: str, decimals: int) -> Decimal:
    raw = Decimal(int(raw_value or "0"))
    if decimals <= 0:
        return raw
    return raw / (Decimal(10) ** decimals)


def sort_units(amount: Decimal) -> str:
    """Signed integer scaled by 1e36 so the browser can compare with BigInt."""
    units = (amount * SORT_SCALE).to_integral_value(rounding=ROUND_HALF_UP)
    return format(units, "f")


def format_amount(amount: Decimal) -> str:
    """Trimmed decimal with thousands separators. Never shows 0 for a non-zero value."""
    if amount == 0:
        return "0"
    negative = amount < 0
    magnitude = -amount if negative else amount
    exact = format(magnitude, "f")
    if "." in exact:
        whole, frac = exact.split(".")
        trimmed = frac.rstrip("0")
        if len(trimmed) > 10:
            short = trimmed[:10].rstrip("0")
            if not short or Decimal(f"{whole}.{short}") == 0:
                shown = trimmed
            else:
                shown = short
        else:
            shown = trimmed
        body = whole if not shown else f"{whole}.{shown}"
    else:
        body = exact
    whole, dot, frac = body.partition(".")
    grouped = f"{int(whole):,}"
    if dot:
        grouped = f"{grouped}.{frac}"
    return f"-{grouped}" if negative else grouped


def exact_amount(amount: Decimal) -> str:
    return format(amount, "f")


def pretty_method(function_name: str) -> str:
    name = (function_name or "").split("(", 1)[0].strip()
    if not name:
        return ""
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", name)
    if not parts:
        return name
    words = [_ACRONYMS.get(part.lower(), part.capitalize()) for part in parts]
    return " ".join(words)


def token_display(name: str, symbol: str) -> str:
    name = (name or "").strip()
    symbol = (symbol or "").strip()
    if name and symbol:
        return f"{name} ({symbol})"
    return name or symbol or "Unknown token"


def clean_api_key(value: str) -> str:
    text = "".join(ch for ch in (value or "").strip() if ch.isprintable())
    if any(ch.isspace() for ch in text) or not 8 <= len(text) <= 200:
        raise ValueError("Enter an Etherscan API key. It cannot contain spaces.")
    return text


def mask_secret(secret: str) -> str:
    if len(secret) < 8:
        return "••••"
    return f"{secret[:4]}…{secret[-4:]}"


def clean_label(label: str) -> str:
    text = "".join(ch for ch in (label or "").strip() if ch.isprintable())
    if len(text) > 80:
        raise ValueError("Label must be 80 characters or fewer.")
    return text


def _looks_like_key(value: str) -> bool:
    text = value.strip()
    return bool(text) and not any(ch.isspace() for ch in text) and 8 <= len(text) <= 200


def _split_key_line(line: str) -> tuple[str, str]:
    for separator in ("\t", "|", ","):
        if separator in line:
            left, right = (part.strip() for part in line.split(separator, 1))
            if _looks_like_key(right) and not _looks_like_key(left):
                return right, left
            return left, right
    return line.strip(), ""


def parse_api_key_file(text: str) -> tuple[list[tuple[str, str]], int]:
    """Read keys from a text file.

    Each line is one key. A comma, tab, or pipe can add a label on either
    side of the key. Blank lines and ``#`` comments are skipped. Repeated
    keys in the file count once. The second value is how many lines were
    not keys.
    """
    seen: set[str] = set()
    entries: list[tuple[str, str]] = []
    invalid = 0
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        key_text, label_text = _split_key_line(line)
        try:
            secret = clean_api_key(key_text)
            label = clean_label(label_text)
        except ValueError:
            invalid += 1
            continue
        if secret in seen:
            continue
        seen.add(secret)
        entries.append((secret, label))
    return entries, invalid


def describe_key_import(added: int, existing: int, invalid: int) -> str:
    if added == 0 and existing and invalid == 0:
        if existing == 1:
            return "1 key was already saved."
        return f"{existing} keys were already saved."
    parts: list[str] = []
    if added == 1:
        parts.append("Added 1 key")
    elif added:
        parts.append(f"Added {added} keys")
    if existing:
        parts.append(f"{existing} already saved")
    if invalid == 1:
        parts.append("Skipped 1 invalid line")
    elif invalid:
        parts.append(f"Skipped {invalid} invalid lines")
    if not parts:
        return "No API keys found. Put one key on each line."
    return ". ".join(parts) + "."
