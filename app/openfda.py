"""openFDA drug shortages API client.

Fetches https://api.fda.gov/drug/shortages.json with pagination and normalizes
each result to a flat dict of the fields our schema cares about.

The endpoint mirrors FDA's Drug Shortages database. Because field naming for
this (newer) endpoint varies in the wild documentation (e.g. ``update_type``
vs ``type_of_update``), normalization accepts known aliases and the full raw
payload is always preserved by the sync layer in ``raw_json``.
"""

from datetime import date, datetime
from typing import Any, Iterator

import httpx

BASE_URL = "https://api.fda.gov/drug/shortages.json"
PAGE_SIZE = 1000  # openFDA maximum
# openFDA refuses skip > 25000; the shortage dataset is ~2k records so this
# is a safety valve, not an expected limit.
MAX_SKIP = 25000

# canonical field -> accepted aliases in the API payload, tried in order
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "generic_name": ("generic_name",),
    "proprietary_name": ("proprietary_name", "brand_name"),
    "company_name": ("company_name",),
    "status": ("status",),
    "initial_posting_date": ("initial_posting_date",),
    "update_date": ("update_date", "date_of_update", "change_date"),
    "update_type": ("update_type", "type_of_update"),
    "discontinued_date": ("discontinued_date", "date_discontinued"),
    "shortage_reason": ("shortage_reason", "reason_for_shortage"),
    "availability": ("availability", "availability_info"),
    "related_info": ("related_info", "related_information"),
    "resolved_note": ("resolved_note",),
    "therapeutic_category": ("therapeutic_category",),
    "strength": ("strength",),
    "dosage_form": ("dosage_form",),
    "presentation": ("presentation",),
    "package_ndc": ("package_ndc",),
    "contact_info": ("contact_info",),
}

DATE_FIELDS = frozenset(
    ("initial_posting_date", "update_date", "discontinued_date")
)


def _coerce_text(value: Any) -> str | None:
    """openFDA returns some fields as lists (e.g. therapeutic_category)."""
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return "; ".join(parts) or None
    text = str(value).strip()
    return text or None


def parse_fda_date(value: Any) -> date | None:
    """Parse openFDA date strings; both YYYY-MM-DD and YYYYMMDD occur."""
    text = _coerce_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one raw API result to canonical fields (plus ``raw`` passthrough)."""
    out: dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        value = None
        for alias in aliases:
            if raw.get(alias) is not None:
                value = raw[alias]
                break
        if field in DATE_FIELDS:
            out[field] = parse_fda_date(value)
        else:
            out[field] = _coerce_text(value)
    out["raw"] = raw
    return out


class OpenFDAClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self._client = client or httpx.Client(timeout=timeout)

    def fetch_page(self, skip: int, limit: int = PAGE_SIZE) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "skip": skip}
        if self.api_key:
            params["api_key"] = self.api_key
        resp = self._client.get(self.base_url, params=params)
        resp.raise_for_status()
        return resp.json()

    def iter_all(self) -> Iterator[dict[str, Any]]:
        """Yield every normalized shortage record, paginating until exhausted."""
        skip = 0
        while skip <= MAX_SKIP:
            payload = self.fetch_page(skip=skip)
            results = payload.get("results", [])
            for raw in results:
                yield normalize_result(raw)
            total = payload.get("meta", {}).get("results", {}).get("total")
            skip += len(results)
            if not results or (total is not None and skip >= total):
                break

    def fetch_all(self) -> list[dict[str, Any]]:
        return list(self.iter_all())

    def close(self) -> None:
        self._client.close()
