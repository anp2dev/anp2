"""On-chain donation verification (PROTOCOL (JP-redacted)13.3).

Public-API only; no operator keys required for the v0.1 default. Adds
informational verification for BTC (mempool.space REST) and ETH/ERC-20
(Etherscan public free tier).

The relay itself does NOT mutate kind 17 events (signed content is
immutable). Instead, /api/verify/<event_id> runs the on-chain check
on-demand and returns the verdict. External verifier AIs (PROTOCOL
(JP-redacted)13.3.4) consume this signal to decide whether to publish a
type=verification attestation under their own Ed25519 key, which the
funding aggregator then trust-weights.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


HTTP_TIMEOUT = 5.0  # seconds; on-chain APIs should respond fast or not at all


def _http_get_json(url: str) -> dict | list | None:
    """GET <url>, parse JSON, return None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "anp2-relay/0.1"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, TimeoutError, OSError):
        return None


def verify_btc(tx_hash: str, expected_amount_sat: int | None = None,
               expected_to_address: str | None = None) -> dict:
    """Verify a BTC transaction via mempool.space (public, no API key).

    Returns:
      {
        "verified": bool,
        "chain": "BTC",
        "tx_hash": str,
        "status": "verified" | "failed" | "pending" | "unverifiable",
        "method": "mempool.space",
        "details": dict | None,
        "note": str,
      }
    """
    tx_hash = tx_hash.lower()
    if len(tx_hash) != 64 or not all(c in "0123456789abcdef" for c in tx_hash):
        return {"verified": False, "chain": "BTC", "tx_hash": tx_hash,
                "status": "failed", "method": "mempool.space", "details": None,
                "note": "tx_hash must be 64 lowercase hex chars"}
    data = _http_get_json(f"https://mempool.space/api/tx/{tx_hash}")
    if data is None or not isinstance(data, dict):
        return {"verified": False, "chain": "BTC", "tx_hash": tx_hash,
                "status": "failed", "method": "mempool.space", "details": None,
                "note": "mempool.space returned no/invalid data"}
    confirmed = bool(data.get("status", {}).get("confirmed"))
    if not confirmed:
        return {"verified": False, "chain": "BTC", "tx_hash": tx_hash,
                "status": "pending", "method": "mempool.space", "details": data,
                "note": "tx exists but is not yet confirmed"}
    # Optional amount + recipient checks
    if expected_to_address or expected_amount_sat:
        matched = False
        for vout in data.get("vout") or []:
            addr = vout.get("scriptpubkey_address")
            val = int(vout.get("value", 0))
            if expected_to_address and addr != expected_to_address:
                continue
            if expected_amount_sat and val != expected_amount_sat:
                continue
            matched = True
            break
        if not matched:
            return {"verified": False, "chain": "BTC", "tx_hash": tx_hash,
                    "status": "failed", "method": "mempool.space",
                    "details": data,
                    "note": "tx confirmed but no vout matched expected amount/recipient"}
    return {"verified": True, "chain": "BTC", "tx_hash": tx_hash,
            "status": "verified", "method": "mempool.space",
            "details": {"block_height": data.get("status", {}).get("block_height")},
            "note": "confirmed on-chain via mempool.space"}


def verify_eth(tx_hash: str, expected_amount_wei: int | None = None,
               expected_to_address: str | None = None) -> dict:
    """Verify an ETH (or ERC-20) transaction via Etherscan.

    Reads ANP2_ETHERSCAN_API_KEY for higher rate limits; falls back
    to the public no-key tier (5 req/sec, lower reliability). Returns
    the same envelope shape as `verify_btc`.
    """
    tx_hash = tx_hash.lower()
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66
            and all(c in "0123456789abcdef" for c in tx_hash[2:])):
        return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                "status": "failed", "method": "etherscan", "details": None,
                "note": "tx_hash must be 0x + 64 hex"}
    key = os.environ.get("ANP2_ETHERSCAN_API_KEY", "")
    suffix = f"&apikey={key}" if key else ""
    receipt = _http_get_json(
        f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionReceipt"
        f"&txhash={tx_hash}{suffix}"
    )
    if receipt is None or not isinstance(receipt, dict):
        return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                "status": "failed", "method": "etherscan", "details": None,
                "note": "etherscan returned no data"}
    result = receipt.get("result")
    if result is None:
        return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                "status": "pending", "method": "etherscan", "details": receipt,
                "note": "transaction not yet mined"}
    status_hex = result.get("status")
    if status_hex != "0x1":
        return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                "status": "failed", "method": "etherscan", "details": result,
                "note": f"transaction failed on-chain (status={status_hex})"}
    if expected_to_address:
        to_addr = (result.get("to") or "").lower()
        if to_addr != expected_to_address.lower():
            return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                    "status": "failed", "method": "etherscan", "details": result,
                    "note": f"to-address mismatch: on-chain {to_addr}"}
    # ETH amount check: pull the transaction body for value
    if expected_amount_wei:
        tx = _http_get_json(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash"
            f"&txhash={tx_hash}{suffix}"
        )
        if tx and isinstance(tx, dict) and tx.get("result", {}).get("value"):
            on_chain_wei = int(tx["result"]["value"], 16)
            if on_chain_wei != expected_amount_wei:
                return {"verified": False, "chain": "ETH", "tx_hash": tx_hash,
                        "status": "failed", "method": "etherscan",
                        "details": tx,
                        "note": f"amount mismatch: on-chain {on_chain_wei} wei"}
    return {"verified": True, "chain": "ETH", "tx_hash": tx_hash,
            "status": "verified", "method": "etherscan",
            "details": {"block_number": result.get("blockNumber")},
            "note": "confirmed on-chain via etherscan"}


def verify_lightning(*_, **__) -> dict:
    """Lightning is structurally unverifiable (instant-settle, no public ledger)
    per PROTOCOL (JP-redacted)13.3.1."""
    return {"verified": False, "chain": "lightning", "tx_hash": None,
            "status": "unverifiable", "method": None, "details": None,
            "note": "Lightning has no public ledger (JP-redacted) verification deferred to receiver self-report (LNURL-verify in v0.2+)"}


def verify_donation(payload: dict) -> dict:
    """Dispatch to the per-chain verifier based on `payload['chain']`."""
    chain = (payload.get("chain") or "").lower()
    tx = payload.get("tx_hash") or ""
    if chain == "btc":
        amt = payload.get("amount_sat") or payload.get("amount")
        try:
            amt = int(amt) if amt is not None else None
        except (ValueError, TypeError):
            amt = None
        return verify_btc(tx, expected_amount_sat=amt,
                          expected_to_address=payload.get("to_address"))
    if chain in ("eth", "ethereum"):
        amt = payload.get("amount_wei")
        try:
            amt = int(amt) if amt is not None else None
        except (ValueError, TypeError):
            amt = None
        return verify_eth(tx, expected_amount_wei=amt,
                          expected_to_address=payload.get("to_address"))
    if chain == "lightning":
        return verify_lightning()
    return {"verified": False, "chain": chain, "tx_hash": tx,
            "status": "unverifiable", "method": None, "details": None,
            "note": f"no v0.1 on-chain verifier for chain={chain!r}"}
