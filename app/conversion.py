"""
POS conversion correlation — two methods:

1. Time-window (baseline): a customer session that had billing-zone presence
   within the 5 minutes before a POS transaction → converted.

2. Brand-dwell (differentiator): session dwelled in a brand zone whose name
   matches the purchased brand → brand-attributed conversion.

The combined converted set (union of both methods) is used for conversion_rate
and the Purchase stage of the funnel.
"""
import glob
import logging
import os
from datetime import datetime, timedelta, timezone
import pandas as pd

logger = logging.getLogger("store_intelligence")

# Brand name → zone_id mapping (POS brand_name → pipeline zone_id)
_BRAND_TO_ZONE: dict[str, str] = {
    "DERMDOC": "DERMDOC",
    "DermDoc": "DERMDOC",
    "Good Vibes": "GOOD_VIBES",
    "Minimalist": "MINIMALIST",
    "Aqualogica": "AQUALOGICA",
    "Lakme": "LAKME",
    "Lakme Skin": "LAKME",
    "Maybelline": "MAYBELLINE",
    "Faces Canada": "FACES_CANADA",
    "Colorbar": "COLORBAR_SUGAR",
    "Sugar": "COLORBAR_SUGAR",
    "Swiss Beauty": "SWISS_BEAUTY",
    "Renee": "RENEE_NY_BAE",
    "NY Bae": "RENEE_NY_BAE",
    "Alps Goodness": "ALPS_GOODNESS",
    "Streax": "STREAX",
    "The Face Shop": "THE_FACE_SHOP",
    "EB Korean": "EB_KOREAN",
    "Accessories": "ACCESSORIES",
}

_BILLING_ZONES = {"CASH_COUNTER"}
_CONVERSION_WINDOW_MINUTES = 5


def _load_pos_df(data_dir: str = "data") -> pd.DataFrame:
    """Load and parse the POS CSV, returning a clean DataFrame."""
    pattern = os.path.join(data_dir, "Brigade_Bangalore_*.csv")
    matches = glob.glob(pattern)
    if not matches:
        logger.warning("No POS CSV found at %s", pattern)
        return pd.DataFrame()

    df = pd.read_csv(matches[0], dtype=str)
    # Parse timestamp: order_date (DD-MM-YYYY) + order_time (HH:MM:SS)
    df["order_dt"] = pd.to_datetime(
        df["order_date"].str.strip() + " " + df["order_time"].str.strip(),
        format="%d-%m-%Y %H:%M:%S",
        errors="coerce",
    ).dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC")
    df = df.dropna(subset=["order_dt"])
    df["brand_zone"] = df["brand_name"].str.strip().map(_BRAND_TO_ZONE)
    return df




def _parse_ts(ts_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_converted_visitor_ids(conn, store_id: str, data_dir: str = "data") -> set[str]:
    """
    Return the set of visitor_ids (non-staff customers) attributed to at least
    one POS transaction via time-window OR brand-dwell matching.
    """
    pos = _load_pos_df(data_dir)
    if pos.empty:
        return set()

    pos_store = pos[pos.get("store_id", pd.Series(dtype=str)).str.strip() == store_id] if "store_id" in pos.columns else pos
    if pos_store.empty:
        pos_store = pos  # fallback: use all POS rows (single-store dataset)

    # --- Load billing-zone events for time-window method ---
    billing_rows = conn.execute(
        """SELECT visitor_id, timestamp FROM events
           WHERE store_id=? AND is_staff=0
             AND (event_type='BILLING_QUEUE_JOIN'
                  OR (event_type IN ('ZONE_ENTER','ZONE_EXIT','ZONE_DWELL')
                      AND zone_id = 'CASH_COUNTER'))
           ORDER BY timestamp""",
        (store_id,),
    ).fetchall()

    billing_events: list[tuple[str, datetime]] = []
    for row in billing_rows:
        ts = _parse_ts(row["timestamp"])
        if ts:
            billing_events.append((row["visitor_id"], ts))

    # --- Load brand-dwell events for brand-match method ---
    dwell_rows = conn.execute(
        """SELECT visitor_id, zone_id, timestamp FROM events
           WHERE store_id=? AND is_staff=0
             AND event_type IN ('ZONE_ENTER','ZONE_DWELL')
             AND zone_id IS NOT NULL""",
        (store_id,),
    ).fetchall()

    # visitor_id → set of zones they dwelled in
    visitor_zones: dict[str, set[str]] = {}
    for row in dwell_rows:
        visitor_zones.setdefault(row["visitor_id"], set()).add(row["zone_id"])

    converted: set[str] = set()
    window = timedelta(minutes=_CONVERSION_WINDOW_MINUTES)

    for _, tx in pos_store.iterrows():
        tx_time: datetime = tx["order_dt"]
        brand_zone: str | None = tx.get("brand_zone")

        # Method 1: time-window — was a customer in billing zone 5 min before this tx?
        for vis_id, billing_ts in billing_events:
            if tx_time - window <= billing_ts <= tx_time:
                converted.add(vis_id)

        # Method 2: brand-dwell — did any customer dwell in the brand's zone?
        if brand_zone and not pd.isna(brand_zone):
            for vis_id, zones in visitor_zones.items():
                if brand_zone in zones:
                    converted.add(vis_id)

    return converted


def compute_conversion_rate(conn, store_id: str, data_dir: str = "data") -> float:
    """Conversion rate = converted visitors / unique customer visitors."""
    unique = conn.execute(
        """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
           WHERE store_id=? AND is_staff=0 AND event_type IN ('ENTRY','REENTRY')""",
        (store_id,),
    ).fetchone()["cnt"]
    if unique == 0:
        return 0.0
    converted = get_converted_visitor_ids(conn, store_id, data_dir)
    return round(len(converted) / unique, 4)
