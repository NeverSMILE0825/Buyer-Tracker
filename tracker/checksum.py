"""EIP-55 checksums. Keccak comes from PyCryptodome so addresses match Etherscan."""

from Crypto.Hash import keccak

_HEX = set("0123456789abcdef")


def is_address(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) != 42 or not text.startswith(("0x", "0X")):
        return False
    return all(ch.lower() in _HEX for ch in text[2:])


def to_checksum_address(address: str) -> str:
    if not is_address(address):
        raise ValueError("Enter a wallet address (0x and 40 hex characters).")
    hexaddr = address.strip()[2:].lower()
    digest = keccak.new(digest_bits=256)
    digest.update(hexaddr.encode("ascii"))
    hashed = digest.hexdigest()
    chars = [
        ch.upper() if ch.isalpha() and int(hashed[i], 16) >= 8 else ch
        for i, ch in enumerate(hexaddr)
    ]
    return "0x" + "".join(chars)


def normalize_address(address: str) -> str:
    return to_checksum_address(address)
