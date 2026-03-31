"""
ATProto publisher — posts completed tips as com.linkedclaims.claim records.

Uses the @cooperation/claim-atproto SDK (via Node.js subprocess) for claim
building, validation, and publishing. The SDK ensures claims conform to the
DIF Labs LinkedClaims specification.

Claim mapping (tip → LinkedClaims):
  subject    = tipper DID (who made the tip — the person asserting value)
  claimType  = "tip"
  object     = receiver name (whose work is being valued)
  statement  = human-readable: "Tipped $50.00 — great article!"
  source.uri = content URL (what was tipped for)
  source.howKnown = "FIRST_HAND" (tipper directly valued this)
  confidence = 1.0 (tip is a concrete action, not speculation)
  effectiveDate = when the tip was created
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from config import settings

log = logging.getLogger("simpletip.atproto")

# Path to the Node.js publish script that uses claim-atproto SDK
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PUBLISH_SCRIPT = os.path.join(_SCRIPT_DIR, "publish-claim.mjs")


async def _run_sdk(tip_data: dict) -> dict:
    """Call the claim-atproto SDK via Node.js subprocess.

    Returns {"uri": "at://...", "cid": "..."} on success.
    Raises RuntimeError on failure.
    """
    env = {
        **os.environ,
        "ATPROTO_HANDLE": settings.atproto_handle,
        "ATPROTO_APP_PASSWORD": settings.atproto_app_password,
        "ATPROTO_SERVICE": settings.atproto_service,
    }

    proc = await asyncio.create_subprocess_exec(
        "node", _PUBLISH_SCRIPT, json.dumps(tip_data),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode().strip() if stderr else "unknown error"
        try:
            err_data = json.loads(err_msg)
            err_msg = err_data.get("error", err_msg)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"claim-atproto SDK error: {err_msg}")

    result = json.loads(stdout.decode().strip())
    return result


# ── Publish a single tip ─────────────────────────────────────

async def publish_tip(tip_id, conn) -> str | None:
    """Build and publish a tip as a LinkedClaim via the SDK. Returns AT-URI or None on failure."""
    tip = await conn.fetchrow(
        "SELECT t.id, t.wallet_id, t.amount_cents, t.comment, t.page_url, t.created_at, t.atproto_uri "
        "FROM tips t WHERE t.id = $1 AND t.status = 'completed'",
        tip_id,
    )
    if not tip:
        log.warning("publish_tip: tip %s not found or not completed", tip_id)
        return None
    if tip["atproto_uri"]:
        log.debug("publish_tip: tip %s already published: %s", tip_id, tip["atproto_uri"])
        return tip["atproto_uri"]

    # Get tipper info from wallet_contacts
    tipper_info = await conn.fetchrow(
        "SELECT wc.did, wc.handle, wc.name, wc.display_name, wc.anonymous "
        "FROM wallet_contacts wc "
        "WHERE wc.wallet_id = $1",
        tip["wallet_id"],
    )

    splits = await conn.fetch(
        "SELECT r.slug, r.name, ts.amount_cents, ts.role "
        "FROM tip_splits ts JOIN receivers r ON r.id = ts.receiver_id "
        "WHERE ts.tip_id = $1",
        tip_id,
    )
    if not splits:
        log.warning("publish_tip: tip %s has no splits", tip_id)
        return None

    # ── Build claim data for the SDK ─────────────────────────

    # Subject = the tipper (who is making the claim with their money)
    tipper_did = ""
    if tipper_info and not tipper_info["anonymous"]:
        tipper_did = tipper_info["did"] or tipper_info["handle"] or ""

    # Object = the receiver (whose work is being valued)
    receiver_names = " + ".join(s["name"] or s["slug"] for s in splits)

    # Statement = human-readable description
    amount_str = f"${tip['amount_cents'] / 100:.2f}"
    statement = f"Tipped {amount_str} to {receiver_names}"
    if tip["comment"]:
        statement += f" — {tip['comment']}"

    # Timestamps
    created_at = tip["created_at"]
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        iso_ts = created_at.isoformat(timespec="milliseconds")
    else:
        iso_ts = str(created_at)

    # Data passed to the SDK script
    tip_data = {
        "subject": tipper_did,
        "object": receiver_names,
        "statement": statement,
        "effectiveDate": iso_ts,
        "createdAt": iso_ts,
    }
    if tip["page_url"]:
        tip_data["sourceUri"] = tip["page_url"]

    # Publish via claim-atproto SDK
    result = await _run_sdk(tip_data)

    at_uri = result.get("uri", "")
    if at_uri:
        await conn.execute("UPDATE tips SET atproto_uri = $1 WHERE id = $2", at_uri, tip_id)
        log.info("Published tip %s → %s (via claim-atproto SDK)", tip_id, at_uri)

    return at_uri


# ── Batch publish ────────────────────────────────────────────

async def publish_batch(conn, since_hours: int = 24) -> dict:
    """Publish all unpublished completed tips from the last N hours. Returns summary."""
    rows = await conn.fetch(
        "SELECT id FROM tips "
        "WHERE status = 'completed' AND atproto_uri IS NULL "
        "AND created_at > now() - make_interval(hours => $1) "
        "ORDER BY created_at ASC",
        since_hours,
    )

    published = 0
    failed = 0
    errors = []

    for row in rows:
        try:
            uri = await publish_tip(row["id"], conn)
            if uri:
                published += 1
            else:
                failed += 1
                errors.append(f"tip {row['id']}: returned None")
        except Exception as e:
            failed += 1
            errors.append(f"tip {row['id']}: {e}")
            log.exception("Batch publish failed for tip %s", row["id"])

    return {"published": published, "failed": failed, "errors": errors}


# ── Batch summary claim ──────────────────────────────────────

async def publish_summary(conn, since_hours: int = 24) -> str | None:
    """Publish a summary claim for high-volume periods.

    Instead of one claim per tip, this creates a single claim summarizing
    all tips in the time window. Used when tip volume is high.
    """
    rows = await conn.fetch(
        "SELECT t.id, t.amount_cents, t.page_url, t.created_at, "
        "       array_agg(DISTINCT r.name) AS receiver_names "
        "FROM tips t "
        "JOIN tip_splits ts ON ts.tip_id = t.id "
        "JOIN receivers r ON r.id = ts.receiver_id "
        "WHERE t.status = 'completed' AND t.atproto_uri IS NULL "
        "AND t.created_at > now() - make_interval(hours => $1) "
        "GROUP BY t.id ORDER BY t.created_at ASC",
        since_hours,
    )

    if not rows:
        return None

    total_cents = sum(r["amount_cents"] for r in rows)
    total_str = f"${total_cents / 100:.2f}"
    tip_count = len(rows)

    # Collect unique receivers
    all_receivers = set()
    for r in rows:
        for name in r["receiver_names"]:
            if name:
                all_receivers.add(name)
    receivers_str = ", ".join(sorted(all_receivers))

    now_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    tip_data = {
        "subject": settings.node_url,
        "object": receivers_str,
        "statement": f"Summary: {tip_count} tips totaling {total_str} to {receivers_str}",
        "effectiveDate": now_ts,
        "createdAt": now_ts,
        "sourceUri": settings.node_url,
    }

    result = await _run_sdk(tip_data)

    at_uri = result.get("uri", "")
    if at_uri:
        tip_ids = [r["id"] for r in rows]
        await conn.execute(
            "UPDATE tips SET atproto_uri = $1 WHERE id = ANY($2::uuid[])",
            at_uri, tip_ids,
        )
        log.info("Published summary claim for %d tips → %s (via SDK)", tip_count, at_uri)

    return at_uri
