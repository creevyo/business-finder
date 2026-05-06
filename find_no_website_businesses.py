#!/usr/bin/env python3
"""
Find local businesses with no website using Google Places API (New).
Outputs a CSV with competitor name-drops for cold outreach.
"""

import csv
import math
import os
import sys
import time
from collections import Counter
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

PLACES_API_BASE = "https://places.googleapis.com/v1"

TEXT_SEARCH_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.internationalPhoneNumber",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.websiteUri",
    "places.location",
    "places.types",
    "nextPageToken",
])

NEARBY_FIELD_MASK = "places.id,places.displayName,places.websiteUri"


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        key = input("Enter your Google Places API key: ").strip()
    if not key:
        sys.exit("No API key provided — exiting.")
    return key


def text_search(api_key: str, query: str, page_token: Optional[str] = None) -> dict:
    url = f"{PLACES_API_BASE}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    body: dict = {"textQuery": query}
    if page_token:
        body["pageToken"] = page_token

    resp = requests.post(url, json=body, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def nearby_search(
    api_key: str,
    lat: float,
    lng: float,
    primary_type: Optional[str],
    radius: float = 5000.0,
) -> List[dict]:
    url = f"{PLACES_API_BASE}/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": NEARBY_FIELD_MASK,
    }
    body: dict = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius,
            }
        },
        "maxResultCount": 20,
    }
    if primary_type:
        body["includedPrimaryTypes"] = [primary_type]

    resp = requests.post(url, json=body, headers=headers, timeout=15)

    # If the type was rejected, retry without it
    if resp.status_code == 400 and primary_type:
        body.pop("includedPrimaryTypes", None)
        resp = requests.post(url, json=body, headers=headers, timeout=15)

    if not resp.ok:
        return []

    return resp.json().get("places", [])


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def display_name(place: dict) -> str:
    dn = place.get("displayName", {})
    if isinstance(dn, dict):
        return dn.get("text", "")
    return str(dn)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_from_pool(
    target: dict,
    pool: List[dict],
    count: int = 2,
) -> List[dict]:
    """Return the `count` closest places from pool to the target location."""
    loc = target.get("location", {})
    if not loc:
        return []
    tlat, tlng = loc.get("latitude", 0.0), loc.get("longitude", 0.0)

    scored = []
    for p in pool:
        ploc = p.get("location", {})
        if not ploc:
            continue
        dist = haversine_km(tlat, tlng, ploc.get("latitude", 0.0), ploc.get("longitude", 0.0))
        scored.append((dist, p))

    scored.sort(key=lambda x: x[0])
    return [p for _, p in scored[:count]]


def detect_primary_type(places: List[dict]) -> Optional[str]:
    counts: Counter = Counter()
    for p in places:
        types = p.get("types", [])
        if types:
            counts[types[0]] += 1
    return counts.most_common(1)[0][0] if counts else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key = get_api_key()

    print()
    business_type = input("Business type (e.g. plumber): ").strip()
    location_name = input("Location       (e.g. Wirral): ").strip()

    if not business_type or not location_name:
        sys.exit("Business type and location are required.")

    query = f"{business_type} in {location_name}"
    print(f"\nSearching: '{query}'")
    print("Paginating through all results — please wait…\n")

    all_places: List[dict] = []
    page_token: Optional[str] = None
    page_num = 0

    while True:
        page_num += 1
        print(f"  Page {page_num}… ", end="", flush=True)
        try:
            data = text_search(api_key, query, page_token)
        except requests.HTTPError as exc:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
            print(f"\nAPI error on page {page_num}: {detail}")
            break
        except requests.RequestException as exc:
            print(f"\nNetwork error: {exc}")
            break

        batch = data.get("places", [])
        all_places.extend(batch)
        print(f"{len(batch)} result(s)")

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(2)  # Google requires a short pause between paginated calls

    print(f"\nTotal fetched : {len(all_places)}")

    no_website   = [p for p in all_places if not p.get("websiteUri")]
    with_website = [p for p in all_places if     p.get("websiteUri")]

    print(f"  No website   : {len(no_website)}")
    print(f"  Has website  : {len(with_website)}")

    if not no_website:
        print("\nNo businesses without websites found. Nothing to export.")
        return

    primary_type = detect_primary_type(all_places)
    print(f"  Primary type : {primary_type or '(unknown)'}")

    rows: List[Dict] = []
    print(f"\nFinding competitors for {len(no_website)} business(es)…\n")

    for i, place in enumerate(no_website):
        name        = display_name(place) or "N/A"
        phone       = place.get("internationalPhoneNumber", "")
        review_cnt  = place.get("userRatingCount", "")
        maps_url    = place.get("googleMapsUri", "")
        place_id    = place.get("id", "")
        loc         = place.get("location", {})

        print(f"  [{i + 1}/{len(no_website)}] {name}")

        # Step 1 — pull nearest competitors from already-fetched pool (no extra API cost)
        competitors = nearest_from_pool(place, with_website, count=2)
        used_ids = {c.get("id") for c in competitors}

        # Step 2 — if still short, call Nearby Search for more
        if len(competitors) < 2 and loc:
            nearby = nearby_search(
                api_key,
                loc.get("latitude", 0.0),
                loc.get("longitude", 0.0),
                primary_type,
            )
            time.sleep(0.3)

            for p in nearby:
                if len(competitors) >= 2:
                    break
                pid = p.get("id")
                if pid == place_id or pid in used_ids:
                    continue
                if p.get("websiteUri"):
                    competitors.append(p)
                    used_ids.add(pid)

        comp1 = competitors[0] if len(competitors) > 0 else {}
        comp2 = competitors[1] if len(competitors) > 1 else {}

        rows.append({
            "Business Name"    : name,
            "Phone Number"     : phone,
            "Review Count"     : review_cnt,
            "Google Maps URL"  : maps_url,
            "Competitor 1 Name"   : display_name(comp1),
            "Competitor 1 Website": comp1.get("websiteUri", ""),
            "Competitor 2 Name"   : display_name(comp2),
            "Competitor 2 Website": comp2.get("websiteUri", ""),
        })

    # Write CSV
    safe_type = business_type.replace(" ", "_").lower()
    safe_loc  = location_name.replace(" ", "_").lower()
    output_file = f"{safe_type}_{safe_loc}_no_website.csv"

    fieldnames = [
        "Business Name", "Phone Number", "Review Count", "Google Maps URL",
        "Competitor 1 Name", "Competitor 1 Website",
        "Competitor 2 Name", "Competitor 2 Website",
    ]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nExported {len(rows)} row(s) → {output_file}")


if __name__ == "__main__":
    main()
