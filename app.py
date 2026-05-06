import csv
import io
import math
import time
from collections import Counter
from typing import Dict, List, Optional

import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

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

NEARBY_FIELD_MASK = "places.id,places.displayName,places.websiteUri,places.location"


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def text_search(query: str, page_token: Optional[str] = None) -> dict:
    url = f"{PLACES_API_BASE}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    body: dict = {"textQuery": query}
    if page_token:
        body["pageToken"] = page_token
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def nearby_search(lat: float, lng: float, primary_type: Optional[str], radius: float = 5000.0) -> List[dict]:
    url = f"{PLACES_API_BASE}/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
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


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_from_pool(target: dict, pool: List[dict], count: int = 2) -> List[dict]:
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


def run_search(business_type: str, location: str, status_text, progress_bar) -> List[Dict]:
    query = f"{business_type} in {location}"
    status_text.text(f"Searching for '{query}'...")

    all_places: List[dict] = []
    page_token: Optional[str] = None
    page_num = 0

    while True:
        page_num += 1
        status_text.text(f"Fetching page {page_num}...")
        try:
            data = text_search(query, page_token)
        except requests.HTTPError as exc:
            try:
                detail = exc.response.json()
                msg = detail.get("error", {}).get("message", str(exc))
            except Exception:
                msg = str(exc)
            st.error(f"API error: {msg}")
            return []
        except requests.RequestException as exc:
            st.error(f"Network error: {exc}")
            return []

        batch = data.get("places", [])
        all_places.extend(batch)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(2)

    no_website = [p for p in all_places if not p.get("websiteUri")]
    with_website = [p for p in all_places if p.get("websiteUri")]
    primary_type = detect_primary_type(all_places)

    rows: List[Dict] = []
    total = len(no_website)

    for i, place in enumerate(no_website):
        name = display_name(place) or "N/A"
        status_text.text(f"Finding competitors for {name}...")
        progress_bar.progress((i + 1) / max(total, 1))

        phone = place.get("internationalPhoneNumber", "")
        review_cnt = place.get("userRatingCount", "")
        maps_url = place.get("googleMapsUri", "")
        place_id = place.get("id", "")
        loc = place.get("location", {})

        competitors = nearest_from_pool(place, with_website, count=2)
        used_ids = {c.get("id") for c in competitors}

        if len(competitors) < 2 and loc:
            nearby = nearby_search(
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
            "Business Name": name,
            "Phone Number": phone,
            "Review Count": review_cnt,
            "Google Maps URL": maps_url,
            "Competitor 1 Name": display_name(comp1),
            "Competitor 1 Website": comp1.get("websiteUri", ""),
            "Competitor 2 Name": display_name(comp2),
            "Competitor 2 Website": comp2.get("websiteUri", ""),
        })

    return rows


def rows_to_csv(rows: List[Dict]) -> str:
    fieldnames = [
        "Business Name", "Phone Number", "Review Count", "Google Maps URL",
        "Competitor 1 Name", "Competitor 1 Website",
        "Competitor 2 Name", "Competitor 2 Website",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Find Businesses Without Websites", page_icon="🔍", layout="centered")

st.title("Find Businesses Without Websites")
st.markdown("Discover local businesses with no online presence — ready for cold outreach.")

if not API_KEY:
    st.error("Missing `GOOGLE_PLACES_API_KEY`. Add it to a `.env` file and restart the app.")
    st.stop()

st.divider()

col1, col2 = st.columns(2)
with col1:
    business_type = st.text_input("Business Type", placeholder="e.g. plumber")
with col2:
    location = st.text_input("Location", placeholder="e.g. Wirral")

search_clicked = st.button("Find Businesses", type="primary", use_container_width=True)

if search_clicked:
    if not business_type.strip() or not location.strip():
        st.warning("Please enter both a business type and a location.")
    else:
        with st.container():
            status_text = st.empty()
            progress_bar = st.progress(0)

            rows = run_search(business_type.strip(), location.strip(), status_text, progress_bar)

            progress_bar.empty()
            status_text.empty()

            if rows:
                st.success(f"Found **{len(rows)}** businesses without a website.")
                st.divider()

                import pandas as pd
                df = pd.DataFrame(rows)
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Google Maps URL": st.column_config.LinkColumn("Google Maps URL"),
                        "Competitor 1 Website": st.column_config.LinkColumn("Competitor 1 Website"),
                        "Competitor 2 Website": st.column_config.LinkColumn("Competitor 2 Website"),
                    },
                )

                safe_type = business_type.strip().replace(" ", "_").lower()
                safe_loc = location.strip().replace(" ", "_").lower()
                filename = f"{safe_type}_{safe_loc}_no_website.csv"

                st.download_button(
                    label="Download CSV",
                    data=rows_to_csv(rows),
                    file_name=filename,
                    mime="text/csv",
                    use_container_width=True,
                )
            else:
                st.info("No businesses without websites found for that search.")
