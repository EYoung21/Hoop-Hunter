"""Check the Hoop Hunter API the way the app uses it: publishable key over REST / RPC.

    pipeline/.venv/Scripts/python supabase/verify_api.py [--env ~/.config/hoop-hunter/supabase.env]

* anon (publishable key, no session): can read courts and call the RPCs; cannot write anything.
* authenticated: a throwaway email/password user is created with the secret key, used to check that a
  player can write only their own reviews and pending courts, and is deleted at the end (its rows cascade).

Reads SUPABASE_URL, SUPABASE_ANON_KEY (publishable) and SUPABASE_SERVICE_ROLE_KEY (secret) from the env file.
Prints no keys. Exit code 1 if any check fails.
"""

import argparse
import secrets
import sys
from pathlib import Path

import requests


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--env", type=Path, default=Path.home() / ".config" / "hoop-hunter" / "supabase.env")
env = load_env(ap.parse_args().env)
URL = env["SUPABASE_URL"]
PUB = env["SUPABASE_ANON_KEY"]
SECRET = env["SUPABASE_SERVICE_ROLE_KEY"]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def anon_headers(**extra):
    return {"apikey": PUB, "Content-Type": "application/json", **extra}


def short(r):
    try:
        j = r.json()
        if isinstance(j, dict):
            return f"{r.status_code} {j.get('code', '')} {j.get('message', j.get('msg', ''))}"[:160]
    except ValueError:
        pass
    return f"{r.status_code} {r.text[:120]}"


def rpc(name, body, headers, offset=None):
    q = f"?offset={offset}&limit=1000" if offset is not None else ""
    return requests.post(f"{URL}/rest/v1/rpc/{name}{q}", json=body, headers=headers, timeout=60)


def all_in_view(box, headers, max_rows=5000):
    rows, offset = [], 0
    while offset < max_rows:
        r = rpc("courts_in_view", {**box, "max_rows": max_rows}, headers, offset=offset)
        r.raise_for_status()
        page = r.json()
        rows += page
        if len(page) < 1000:
            break
        offset += 1000
    assert len({x["id"] for x in rows}) == len(rows), "pages overlap"
    return rows


A = anon_headers()

# ---- anon reads
r = requests.get(f"{URL}/rest/v1/courts?select=id,source,status&limit=3", headers=A, timeout=30)
check("anon can SELECT courts", r.status_code == 200 and len(r.json()) == 3, short(r) if r.status_code != 200 else "3 rows")

chatt = {"min_lon": -85.44, "min_lat": 34.94, "max_lon": -85.06, "max_lat": 35.24}
rows = all_in_view(chatt, A)
srcs = {}
for x in rows:
    srcs[x["source"]] = srcs.get(x["source"], 0) + 1
check("courts_in_view: Chattanooga", len(rows) > 20, f"{len(rows)} courts {srcs}; first: {rows[0]['id']} {rows[0]['name']!r}")
need = {
    "id",
    "lon",
    "lat",
    "name",
    "source",
    "status",
    "confidence",
    "hoops",
    "surface",
    "lit",
    "indoor",
    "address",
    "region",
    "state",
    "site",
    "osm_id",
    "updated",
}
check("courts_in_view returns the card fields", need <= set(rows[0]), sorted(set(rows[0]))[:20].__str__())

la = {"min_lon": -118.6, "min_lat": 33.6, "max_lon": -118.0, "max_lat": 34.5}
rows = all_in_view(la, A)
states = {x["state"] for x in rows}
check(
    "courts_in_view: Los Angeles (paged with offset, 1000/page)",
    len(rows) > 2000 and states == {"CA"},
    f"{len(rows)} courts, states {states}",
)
sf = {"min_lon": -122.55, "min_lat": 37.70, "max_lon": -122.35, "max_lat": 37.82}
rows = all_in_view(sf, A)
check(
    "courts_in_view: San Francisco",
    len(rows) > 100,
    f"{len(rows)} courts, {sum(1 for x in rows if x['source'] == 'detected')} detected",
)

r = rpc("courts_in_view", {**chatt, "max_rows": 5}, A)
check("courts_in_view honours max_rows", r.ok and len(r.json()) == 5, short(r) if not r.ok else "5 rows")

us = {"min_lon": -125.0, "min_lat": 24.0, "max_lon": -66.0, "max_lat": 50.0}
r = rpc("court_clusters", {**us, "zoom": 4}, A)
cl = r.json() if r.ok else []
total = sum(c["court_count"] for c in cl)
largest = max(cl, key=lambda c: c["court_count"]) if cl else None
check(
    "court_clusters: whole US at zoom 4",
    r.ok and 50 < len(cl) < 400 and total > 85000,
    f"{len(cl)} cells, {total} courts, payload {len(r.content)} bytes; largest {largest}",
)
r = rpc("court_clusters", {**us, "zoom": 4, "statuses": ["candidate"]}, A)
check(
    "court_clusters: candidates only",
    r.ok and sum(c["court_count"] for c in r.json()) > 5000,
    f"{sum(c['court_count'] for c in r.json()) if r.ok else short(r)} candidates",
)
r = rpc("court_clusters", {**us, "zoom": 18}, A)
check(
    "court_clusters caps cells at 64x64 even at high zoom",
    r.ok and len(r.json()) <= 64 * 64,
    f"{len(r.json()) if r.ok else short(r)} cells",
)

r = rpc("review_counts", {"court_ids": ["osm-way-1", "det-03ee95d7f1e1"]}, A)
check("anon can call review_counts", r.ok, short(r) if not r.ok else str(r.json()))

# ---- anon writes must fail
fake = {"id": "web-test-1", "source": "web", "status": "verified", "geom": "SRID=4326;POINT(-85.3 35.0)"}
r = requests.post(f"{URL}/rest/v1/courts", json=fake, headers=A, timeout=30)
check("anon INSERT courts is rejected", r.status_code in (401, 403), short(r))
r = requests.patch(f"{URL}/rest/v1/courts?id=eq.nyc-X159", json={"name": "hacked"}, headers=A, timeout=30)
check("anon UPDATE courts is rejected", r.status_code in (401, 403), short(r))
r = requests.delete(f"{URL}/rest/v1/courts?id=eq.nyc-X159", headers=A, timeout=30)
check("anon DELETE courts is rejected", r.status_code in (401, 403), short(r))
r = requests.post(f"{URL}/rest/v1/reviews", json={"court_id": "nyc-X159", "decision": "court"}, headers=A, timeout=30)
check("anon INSERT reviews is rejected (needs a session)", r.status_code in (401, 403), short(r))
r = requests.get(f"{URL}/rest/v1/reviews?select=*", headers=A, timeout=30)
check("anon cannot read reviews", r.status_code in (401, 403), short(r))
r = requests.post(f"{URL}/rest/v1/user_courts", json={"id": "user-test-1", "lon": -85.3, "lat": 35.0}, headers=A, timeout=30)
check("anon INSERT user_courts is rejected", r.status_code in (401, 403), short(r))
r = requests.post(f"{URL}/rest/v1/court_review_counts", json={}, headers=A, timeout=30)
r2 = requests.get(f"{URL}/rest/v1/court_review_counts?select=*", headers=A, timeout=30)
check("anon cannot read the court_review_counts view", r2.status_code in (401, 403, 404), short(r2))

# ---- anonymous sign-in (project setting)
r = requests.post(f"{URL}/auth/v1/signup", json={"data": {}}, headers=A, timeout=30)
anon_enabled = r.ok
print(f"[INFO] anonymous sign-in: {'ENABLED' if anon_enabled else 'DISABLED'} ({short(r)})")
if anon_enabled:
    requests.delete(f"{URL}/auth/v1/admin/users/{r.json()['user']['id']}", headers={"apikey": SECRET}, timeout=30)

# ---- authenticated user (password test user made with the secret key, deleted at the end)
S = {"apikey": SECRET, "Content-Type": "application/json"}
email = f"rls-test-{secrets.token_hex(4)}@example.com"
password = secrets.token_urlsafe(18)
r = requests.post(
    f"{URL}/auth/v1/admin/users", json={"email": email, "password": password, "email_confirm": True}, headers=S, timeout=30
)
check("admin: create test user", r.ok, short(r) if not r.ok else "")
uid = r.json()["id"] if r.ok else None
r = requests.post(
    f"{URL}/auth/v1/admin/users", json={"email": "x" + email, "password": password, "email_confirm": True}, headers=S, timeout=30
)
other_uid = r.json()["id"] if r.ok else None
try:
    r = requests.post(
        f"{URL}/auth/v1/token?grant_type=password", json={"email": email, "password": password}, headers=A, timeout=30
    )
    token = r.json().get("access_token")
    check("test user signs in with the publishable key", bool(token), short(r) if not token else "")
    U = anon_headers(Authorization=f"Bearer {token}")
    UP = {**U, "Prefer": "return=representation"}

    r = requests.post(
        f"{URL}/rest/v1/reviews", json={"court_id": "nyc-X159", "user_id": uid, "decision": "court"}, headers=UP, timeout=30
    )
    check("authenticated: insert own review", r.status_code == 201, short(r))
    created = r.json()[0]["created_at"] if r.status_code == 201 else None

    r = requests.post(
        f"{URL}/rest/v1/reviews?on_conflict=court_id,user_id",
        json={"court_id": "nyc-X159", "user_id": uid, "decision": "home_court", "created_at": "2000-01-01T00:00:00Z"},
        headers={**UP, "Prefer": "return=representation,resolution=merge-duplicates"},
        timeout=30,
    )
    ok = r.status_code in (200, 201) and r.json()[0]["decision"] == "home_court"
    check("authenticated: upsert changes own review (one per court)", ok, short(r))
    if ok:
        check("server owns created_at (client backdate ignored)", r.json()[0]["created_at"] == created, r.json()[0]["created_at"])

    r = requests.post(
        f"{URL}/rest/v1/reviews", json={"court_id": "osm-way-1", "user_id": uid, "decision": "maybe"}, headers=UP, timeout=30
    )
    check("authenticated: bad decision value rejected", r.status_code >= 400, short(r))

    if other_uid:
        r = requests.post(
            f"{URL}/rest/v1/reviews",
            json={"court_id": "nyc-X159", "user_id": other_uid, "decision": "not_court"},
            headers=UP,
            timeout=30,
        )
        check("authenticated: cannot write a review as another user", r.status_code in (401, 403), short(r))

    r = requests.get(f"{URL}/rest/v1/reviews?select=court_id,decision,user_id", headers=U, timeout=30)
    check(
        "authenticated: reads only own reviews",
        r.ok and all(x["user_id"] == uid for x in r.json()),
        f"{len(r.json()) if r.ok else short(r)} rows",
    )

    r = rpc("review_counts", {"court_ids": ["nyc-X159"]}, A)
    check(
        "review_counts aggregates (anon caller)",
        r.ok and r.json() and r.json()[0]["home_court"] == 1,
        str(r.json() if r.ok else short(r)),
    )

    court = {
        "id": "user-rlstest-abc123",
        "user_id": uid,
        "name": "Test court",
        "hoops": 2,
        "surface": "asphalt",
        "lit": True,
        "notes": "test",
        "lon": -85.31,
        "lat": 35.05,
    }
    r = requests.post(f"{URL}/rest/v1/user_courts", json=court, headers=UP, timeout=30)
    check("authenticated: add own court (pending)", r.status_code == 201 and r.json()[0]["status"] == "pending", short(r))
    r = requests.post(
        f"{URL}/rest/v1/user_courts", json={**court, "id": "user-rlstest-abc124", "status": "approved"}, headers=UP, timeout=30
    )
    check("authenticated: cannot self-approve a court", r.status_code in (401, 403), short(r))
    r = requests.patch(
        f"{URL}/rest/v1/user_courts?id=eq.user-rlstest-abc123", json={"status": "approved"}, headers=UP, timeout=30
    )
    check("authenticated: cannot approve via update", r.status_code in (401, 403) or (r.ok and r.json() == []), short(r))
    r = requests.post(
        f"{URL}/rest/v1/user_courts?on_conflict=id",
        json={**court, "hoops": 4},
        headers={**UP, "Prefer": "return=representation,resolution=merge-duplicates"},
        timeout=30,
    )
    check("authenticated: upsert edits own pending court", r.ok and r.json()[0]["hoops"] == 4, short(r))

    r = requests.post(f"{URL}/rest/v1/courts", json=fake, headers=U, timeout=30)
    check("authenticated: INSERT courts is rejected", r.status_code in (401, 403), short(r))
    r = requests.patch(f"{URL}/rest/v1/courts?id=eq.nyc-X159", json={"name": "hacked"}, headers=UP, timeout=30)
    check("authenticated: UPDATE courts is rejected", r.status_code in (401, 403), short(r))

    r = rpc("courts_in_view", {**chatt, "max_rows": 3}, U)
    check("authenticated: courts_in_view works with a session", r.ok and len(r.json()) == 3, short(r) if not r.ok else "")

    r = requests.delete(f"{URL}/rest/v1/reviews?court_id=eq.nyc-X159&user_id=eq.{uid}", headers=UP, timeout=30)
    check("authenticated: delete own review (undo)", r.ok and len(r.json()) == 1, short(r))
    r = requests.delete(f"{URL}/rest/v1/user_courts?id=eq.user-rlstest-abc123", headers=UP, timeout=30)
    check("authenticated: delete own court", r.ok and len(r.json()) == 1, short(r))
finally:
    for u in (uid, other_uid):
        if u:
            r = requests.delete(f"{URL}/auth/v1/admin/users/{u}", headers=S, timeout=30)
            print(f"[INFO] deleted test user: {r.status_code}")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
