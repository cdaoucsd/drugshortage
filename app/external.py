"""Cross-references against other free data sources, computed on demand.

- Alternatives: openFDA NDC directory (/drug/ndc.json) — other labelers
  marketing the same generic that have no Current shortage record here.
- Recalls: openFDA enforcement endpoint (/drug/enforcement.json) — recent
  recalls mentioning the drug.
- ASHP: link-out only. ASHP's machine-readable feed is a licensed product,
  so we deep-link to their public shortage search per drug; if a license is
  ever obtained, implement an importer alongside app/openfda.py and merge
  records in the sync layer.

Results are best-effort enrichment: name matching across FDA datasets is
fuzzy, lookups are cached in-process for CACHE_TTL, and failures degrade to
an "unavailable" payload rather than an error.
"""

import re
import time
import urllib.parse
from typing import Any

import httpx

NDC_URL = "https://api.fda.gov/drug/ndc.json"
ENFORCEMENT_URL = "https://api.fda.gov/drug/enforcement.json"
ASHP_SEARCH_URL = (
    "https://www.ashp.org/drug-shortages/current-shortages/"
    "drug-shortages-list?page=All&search={query}"
)

CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, Any]] = {}

# Dosage-form / filler words stripped from openFDA shortage generic names
# ("Amoxicillin Oral Powder for Suspension" -> "Amoxicillin") so they can be
# matched against the NDC directory's bare active-ingredient names.
_FORM_WORDS = frozenset(
    """
    oral powder for suspension injection injectable tablets tablet capsules
    capsule solution suspension cream ointment gel drops spray patch
    extended release delayed er dr ir chewable sublingual nasal ophthalmic
    otic topical rectal vaginal inhalation aerosol syrup elixir lozenge
    concentrate emulsion kit vial vials usp hcl hydrochloride dimesylate
    sodium potassium sulfate acetate citrate
    """.split()
)

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|co|corp|corporation|company|pharmaceuticals?|pharma|"
    r"laboratories|labs|usa|us|plc|gmbh|sa|ag)\b\.?",
)


def core_drug_name(generic_name: str) -> str:
    """Reduce a shortage generic name to its leading active-ingredient words."""
    words = re.sub(r"[^a-z0-9\s-]", " ", (generic_name or "").lower()).split()
    kept: list[str] = []
    for w in words:
        if w in _FORM_WORDS or any(c.isdigit() for c in w):
            break
        kept.append(w)
    return " ".join(kept) or (words[0] if words else "")


def normalize_company(name: str | None) -> str:
    cleaned = _COMPANY_SUFFIXES.sub("", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def companies_match(a: str | None, b: str | None) -> bool:
    na, nb = normalize_company(a), normalize_company(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def ashp_link(generic_name: str) -> str:
    return ASHP_SEARCH_URL.format(
        query=urllib.parse.quote(core_drug_name(generic_name))
    )


def _cached_get(client: httpx.Client, url: str, params: dict[str, Any]) -> Any:
    key = url + "?" + urllib.parse.urlencode(sorted(params.items()))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    resp = client.get(url, params=params)
    if resp.status_code == 404:
        # openFDA returns 404 for "no matches found"
        value: Any = {"results": []}
    else:
        resp.raise_for_status()
        value = resp.json()
    _cache[key] = (now + CACHE_TTL, value)
    return value


def clear_cache() -> None:
    _cache.clear()


def fetch_alternatives(
    generic_name: str,
    shortage_companies: list[str],
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Labelers in the NDC directory for this generic with no shortage here."""
    term = core_drug_name(generic_name)
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        payload = _cached_get(
            client,
            NDC_URL,
            {"search": f'generic_name:"{term}"', "limit": 100},
        )
    except httpx.HTTPError as exc:
        return {"available": False, "error": str(exc), "search_term": term}
    finally:
        if own_client:
            client.close()

    by_labeler: dict[str, dict[str, Any]] = {}
    for item in payload.get("results", []):
        labeler = item.get("labeler_name")
        if not labeler:
            continue
        entry = by_labeler.setdefault(
            labeler,
            {"labeler": labeler, "products": 0, "dosage_forms": set()},
        )
        entry["products"] += 1
        if item.get("dosage_form"):
            entry["dosage_forms"].add(item["dosage_form"])

    alternatives = [
        {
            "labeler": e["labeler"],
            "products": e["products"],
            "dosage_forms": sorted(e["dosage_forms"]),
        }
        for e in by_labeler.values()
        if not any(companies_match(e["labeler"], c) for c in shortage_companies)
    ]
    alternatives.sort(key=lambda e: (-e["products"], e["labeler"]))
    return {
        "available": True,
        "search_term": term,
        "alternatives": alternatives[:15],
    }


def fetch_recalls(
    generic_name: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Recent enforcement (recall) reports mentioning the drug."""
    term = core_drug_name(generic_name)
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        payload = _cached_get(
            client,
            ENFORCEMENT_URL,
            {
                "search": f'product_description:"{term}"',
                "sort": "recall_initiation_date:desc",
                "limit": 10,
            },
        )
    except httpx.HTTPError as exc:
        return {"available": False, "error": str(exc), "search_term": term}
    finally:
        if own_client:
            client.close()

    def fmt_date(s: str | None) -> str | None:
        if s and re.fullmatch(r"\d{8}", s):
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return s

    recalls = [
        {
            "recalling_firm": r.get("recalling_firm"),
            "product_description": r.get("product_description"),
            "reason": r.get("reason_for_recall"),
            "classification": r.get("classification"),
            "status": r.get("status"),
            "initiated": fmt_date(r.get("recall_initiation_date")),
        }
        for r in payload.get("results", [])
    ]
    return {"available": True, "search_term": term, "recalls": recalls}
