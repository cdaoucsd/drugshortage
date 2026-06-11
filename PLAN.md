# Drug Shortage Tracker — Project Plan

A web application that tracks FDA drug shortages using the openFDA API, lets users
search by drug name, manufacturer, and dates, and sends notifications when shortages
are posted, updated, or resolved.

---

## 1. Data Source: openFDA Drug Shortages API

**Endpoint:** `https://api.fda.gov/drug/shortages.json`

- Backed by FDA's Drug Shortages database; openFDA refreshes it daily.
- No API key required for light use (240 req/min, 1,000 req/day with a free key —
  more than enough since we only need a periodic sync, not per-user calls).
- Standard openFDA query syntax: `search=`, `limit=` (max 1000), `skip=`, `sort=`,
  `count=` for aggregations.

**Key fields per record** (the ones that matter for our features):

| Field | Use |
|---|---|
| `generic_name`, `proprietary_name` | searchable drug name |
| `company_name` | manufacturer search/filter |
| `status` | `Current` / `Resolved` — the core state we track |
| `initial_posting_date` | date of notice |
| `update_date`, `update_type` | detect changes; `update_type: Resolved` ⇒ date of resolve |
| `shortage_reason` | why (e.g., "Demand increase for the drug") |
| `availability`, `related_info`, `resolved_note` | detail page content |
| `therapeutic_category` | category filter |
| `strength`, `dosage_form`, `presentation`, `package_ndc` | identifies the exact product/presentation |
| `contact_info` | manufacturer contact on detail page |
| `openfda.*` (brand_name, manufacturer_name, rxcui, unii, ndc) | cross-linking & better search synonyms |

**Important data modeling note:** one drug shortage is reported as *multiple records*
(one per company per presentation/NDC). The app should group records into a logical
"shortage" per `generic_name` and show per-company/per-presentation rows under it.

---

## 2. Architecture

```
┌─────────────┐     daily/hourly cron      ┌──────────────┐
│  openFDA API │ ─────────────────────────▶ │  Sync worker  │
└─────────────┘                            └──────┬───────┘
                                                  │ upsert + diff
                                                  ▼
                       ┌──────────┐        ┌──────────────┐
                       │ Notifier  │ ◀──── │   Database    │
                       │ (email,   │ events│ (Postgres/    │
                       │  web push)│       │  SQLite)      │
                       └──────────┘        └──────┬───────┘
                                                  │ REST/JSON
                                                  ▼
                                           ┌──────────────┐
                                           │  Web frontend │
                                           │ (search, list,│
                                           │  detail, subs)│
                                           └──────────────┘
```

**Why sync into our own database instead of querying openFDA live:**
1. Notifications require change detection — we must diff today's data against
   yesterday's, which means storing state.
2. Our own DB gives fast fuzzy/full-text search, custom grouping, and history,
   none of which openFDA provides.
3. We stay far under rate limits and the app keeps working if openFDA is down.

### Suggested stack (simple, deployable anywhere)

- **Backend:** Python + FastAPI (or Node + Express — either works; FastAPI chosen
  for built-in OpenAPI docs and easy background jobs).
- **Database:** SQLite to start (zero ops), with SQLAlchemy so we can move to
  Postgres when we need full-text search at scale. SQLite FTS5 covers search initially.
- **Sync job:** APScheduler (in-process cron) hitting openFDA every 6–12 hours,
  paginating with `limit=1000&skip=` until all records are fetched.
- **Frontend:** React + Vite + Tailwind, talking to the backend's JSON API.
  (A server-rendered Jinja UI is a valid simpler alternative for v1.)
- **Email:** SMTP via a provider (Resend/SendGrid/SES) for subscription alerts.

---

## 3. Database Schema (v1)

```sql
-- One row per openFDA record (company × presentation)
shortage_records (
  id PK,
  fda_record_hash TEXT UNIQUE,     -- hash of identifying fields for upsert
  generic_name TEXT, proprietary_name TEXT,
  company_name TEXT,
  status TEXT,                     -- Current | Resolved
  initial_posting_date DATE,
  update_date DATE, update_type TEXT,
  resolved_date DATE,              -- derived: update_date when status flips to Resolved
  shortage_reason TEXT, availability TEXT, related_info TEXT,
  therapeutic_category TEXT,
  strength TEXT, dosage_form TEXT, presentation TEXT, package_ndc TEXT,
  contact_info TEXT,
  raw_json TEXT,                   -- full payload for forward-compat
  first_seen_at, last_seen_at TIMESTAMPS
)

-- Change log: powers notifications + history timeline
shortage_events (
  id PK, record_id FK,
  event_type TEXT,                 -- new_shortage | updated | resolved | reposted
  occurred_at TIMESTAMP,
  diff_json TEXT                   -- which fields changed
)

-- Notification subscriptions
subscriptions (
  id PK,
  email TEXT,
  match_type TEXT,                 -- drug_name | manufacturer | therapeutic_category | all
  match_value TEXT,
  notify_on TEXT,                  -- new | updated | resolved (comma list)
  verified BOOLEAN, created_at, unsubscribe_token TEXT
)
```

---

## 4. Core Features (v1)

### a. Sync & change detection
- Scheduled job pulls the full dataset (currently ~1,500–2,000 records, so a full
  pull is 2 API calls).
- Upsert by record identity (`generic_name + company_name + presentation/NDC`).
- Diff against stored rows → emit `shortage_events`:
  - record not seen before → `new_shortage`
  - `status` Current→Resolved → `resolved` (set `resolved_date`)
  - any tracked field changed → `updated`
  - record disappears from feed → mark stale, keep history.

### b. Search & browse UI
- **Search bar:** matches generic name, brand name, and manufacturer (FTS +
  prefix matching so "amox" finds amoxicillin).
- **Filters:** status (Current/Resolved), manufacturer, therapeutic category,
  date-of-notice range, date-of-resolve range.
- **List view:** grouped by drug; badge for status; columns for first posted,
  last updated, # of manufacturers affected.
- **Detail page:** per-manufacturer/per-presentation table, shortage reason,
  availability notes, contact info, and an event timeline (posted → updates → resolved).

### c. Notifications
- Users subscribe by drug name, manufacturer, category, or "all new shortages",
  choosing which events to receive (new / updated / resolved).
- Email verification + one-click unsubscribe link.
- Notifier runs after each sync, matches new `shortage_events` against
  subscriptions, sends a digest-style email (one email per sync run per user,
  not one per record).

### d. API
- `GET /api/shortages` (search, filters, pagination)
- `GET /api/shortages/{id}` (detail + events)
- `POST /api/subscriptions`, `GET /api/subscriptions/verify`, `DELETE /api/subscriptions/{token}`
- `GET /api/stats` (counts for dashboard)

---

## 5. Suggested Improvements (beyond your requirements)

Ranked roughly by value-to-effort:

1. **Dashboard with trend stats** — current shortage count, new this week,
   resolved this week, longest-running shortages, top affected therapeutic
   categories. openFDA's `count=` queries make some of this nearly free.
2. **Shortage duration tracking** — since we store notice and resolve dates,
   show "median time to resolution" per category and flag drugs that have been
   short for >1 year.
3. **ASHP cross-reference** — ASHP (ashp.org) maintains a second, often earlier
   shortage list. Even just linking out per drug adds clinical value; scraping/
   importing it would make this app better than FDA's own site.
4. **Alternatives suggestion** — for a drug in shortage, list other manufacturers
   of the same generic that are *not* in shortage (cross-reference the openFDA
   NDC endpoint by generic name). Huge value for pharmacists.
5. **Web push / RSS feeds** — per-search RSS feed and browser push as
   no-account-needed notification channels alongside email.
6. **Watchlists with accounts** — let a user (e.g., a pharmacy) maintain a
   formulary list and get a single combined alert view.
7. **History charts** — sparkline of active shortage counts over time (we
   accumulate this for free once syncing daily).
8. **Recall & enforcement overlay** — openFDA also has `/drug/enforcement.json`;
   showing recalls alongside shortages for the same drug gives fuller context.
9. **Public REST API + docs** — since we build the API anyway, publish it.
10. **Mobile-friendly PWA** — installable, offline-readable last sync.

---

## 6. Build Phases

| Phase | Deliverable | Scope |
|---|---|---|
| **1. Data layer** | Sync worker + DB | openFDA client, schema, upsert/diff, event log, seed full dataset |
| **2. API + search UI** | Usable read-only site | FastAPI endpoints, React list/detail/search/filters |
| **3. Notifications** | Email alerts | Subscriptions, verification, post-sync notifier, digests |
| **4. Polish** | Dashboard + extras | Stats dashboard, duration metrics, RSS, deploy (Docker + Fly.io/Render) |

Each phase is independently shippable. Phase 1–2 gives a working searchable
tracker; Phase 3 adds the notification requirement.

---

## 7. Risks / Notes

- **openFDA data lag:** the FDA database updates daily on business days; this app
  can never be more current than FDA's own reporting (ASHP integration, item 3
  above, mitigates this).
- **Record identity is fuzzy:** openFDA shortage records have no stable ID, so
  the upsert hash must be chosen carefully (generic name + company + presentation
  + NDC) and tested against real data for collisions.
- **Email deliverability:** use a real provider with SPF/DKIM from day one.
- **Field availability:** not every record has every field (e.g., `resolved_note`,
  `therapeutic_category` can be missing); the schema treats everything nullable.
