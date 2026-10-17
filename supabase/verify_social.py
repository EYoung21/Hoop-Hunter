"""Check the Hoop Hunter social features the way the app uses them: publishable key + a player session.

    pipeline/.venv/Scripts/python supabase/verify_social.py [--env ~/.config/hoop-hunter/supabase.env]

Five throwaway email/password players are created with the secret key (anonymous sign-ins may be off), used
for every check below, and deleted at the end; their rows cascade, and the script confirms nothing is left.

* RLS and grants: own rows only; no client writes to profiles, check-ins, XP, badges or runs; reports are
  insert-only; private helpers are not callable; nobody can act as another player.
* 13+ gate, generated and custom handles (shape, uniqueness, blocklist).
* Check-ins: one active per player, 2-hour expiry, planned today / tomorrow only, no coordinates stored,
  XP once per court per local day (4 a day), the 2-day purge.
* Streaks over the player's local day, including both sides of local midnight (offsets chosen from the
  server's clock), never moving backwards, the gap reset, -12:00 / +14:00, bad offsets, the 10-courts badge.
* Runs: presets, idempotent post, join / leave / full / cancel, 3-hour expiry, XP once per run, caps.
* XP from reviews (first per candidate court) and approved user courts; Court Hunter for the first finder.
* Follow by code, mutual "friends" names at a court, the attempt limit, unfollow.
* Weekly leaderboards (friends and state), blocking (both directions), reports.

The secret key is used only for the admin user API, to backdate rows (expiry, streak set-up) and to read reports
and court_firsts. Prints no keys or tokens. Exit code 1 if any check fails.
"""

import argparse
import re
import secrets
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
A = {"apikey": PUB, "Content-Type": "application/json"}
S = {"apikey": SECRET, "Content-Type": "application/json"}
results = []
last_date_header = [None]

# One kept-alive connection. Retry only failures before the request is sent (TLS handshake, connect),
# never after (read=0), so no write runs twice.
http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=Retry(total=4, connect=4, read=0, status=0, other=4, backoff_factor=0.5, allowed_methods=None)))


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def short(r):
    try:
        j = r.json()
        if isinstance(j, dict):
            return f"{r.status_code} {j.get('code', '')} {j.get('message', j.get('msg', ''))}"[:160]
    except ValueError:
        pass
    return f"{r.status_code} {r.text[:120]}"


def message(r):
    """The raised message of a failed RPC (e.g. 'age_gate'), or '' when it succeeded."""
    if r.ok:
        return ""
    try:
        return r.json().get("message", "")
    except ValueError:
        return r.text[:80]


def remember_date(r):
    if r.headers.get("Date"):
        last_date_header[0] = r.headers["Date"]
    return r


def rpc(name, body, headers):
    return remember_date(http.post(f"{URL}/rest/v1/rpc/{name}", json=body, headers=headers, timeout=60))


def rest(method, path, headers, **kw):
    return remember_date(http.request(method, f"{URL}/rest/v1/{path}", headers=headers, timeout=60, **kw))


def denied(r):
    return r.status_code in (401, 403)


def server_now() -> datetime:
    """The database's clock, from the API's Date header (1 s resolution)."""
    remember_date(http.get(f"{URL}/rest/v1/", headers=A, timeout=30))
    return parsedate_to_datetime(last_date_header[0]).astimezone(timezone.utc)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def iso(dt: datetime) -> str:
    """UTC with a Z (safe in a URL query string, where + would mean a space)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def local_day(at: datetime, offset: int) -> date:
    return (at.astimezone(timezone.utc) + timedelta(minutes=offset)).date()


def svc_patch(path, body):
    return rest("PATCH", path, {**S, "Prefer": "return=representation"}, json=body)


# ---------------------------------------------------------------------------------------------- set-up
tag = secrets.token_hex(3)
users = {}  # name -> {"id", "email", "password", "h" (headers)}
for name in "ABCDE":
    email = f"social-test-{tag}-{name.lower()}@example.com"
    password = secrets.token_urlsafe(18)
    r = http.post(
        f"{URL}/auth/v1/admin/users", json={"email": email, "password": password, "email_confirm": True}, headers=S, timeout=30
    )
    if r.ok:
        users[name] = {"id": r.json()["id"], "email": email, "password": password}
check("admin: create 5 test players", len(users) == 5, f"{len(users)} created")

user_court_ids = []
try:
    for name, u in users.items():
        r = http.post(
            f"{URL}/auth/v1/token?grant_type=password",
            json={"email": u["email"], "password": u["password"]},
            headers=A,
            timeout=30,
        )
        u["h"] = {**A, "Authorization": f"Bearer {r.json().get('access_token')}"}
    UA, UB, UC, UD, UE = (users[n]["h"] for n in "ABCDE")
    check("test players sign in with the publishable key", all("Bearer None" not in u["h"]["Authorization"] for u in users.values()))

    r = rpc("courts_in_view", {"min_lon": -85.44, "min_lat": 34.94, "max_lon": -85.06, "max_lat": 35.24, "max_rows": 40}, A)
    courts = sorted(r.json(), key=lambda c: c["id"]) if r.ok else []
    X = [c["id"] for c in courts]
    state_of = {c["id"]: c["state"] for c in courts}
    check("found 12+ real courts to check in at (Chattanooga)", len(X) >= 12, f"{len(X)} courts")

    # ------------------------------------------------------------------------------------------ anon + gate
    r = rpc("my_profile", {"utc_offset": 0}, A)
    check("anon (no session) cannot call social RPCs", denied(r), short(r))
    bad = [t for t in ("profiles", "checkins", "xp_events", "user_badges", "runs", "reports") if not denied(rest("GET", f"{t}?select=*", A))]
    check("anon cannot read any social table", not bad, f"readable: {bad}" if bad else "")

    r = rpc("my_profile", {"utc_offset": 0}, UE)
    check("before the 13+ gate: my_profile is null", r.ok and r.json() is None, short(r))
    r1 = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": 0}, UE)
    r2 = rpc("court_runs", {"court": X[0]}, UE)
    r3 = rpc("follow_by_code", {"code": "ABCDEF"}, UE)
    check(
        "before the 13+ gate: check-in, runs and follows are refused (age_gate)",
        all(message(x) == "age_gate" for x in (r1, r2, r3)),
        f"{message(r1)} / {message(r2)} / {message(r3)}",
    )

    prof = {}
    for n in "ABCD":
        r = rpc("confirm_age", {"utc_offset": -240}, users[n]["h"])
        prof[n] = r.json() if r.ok else {}
    ok = all(
        re.fullmatch(r"Hooper\d{4,6}", p.get("handle", "")) and re.fullmatch(r"[2-9A-HJKMNP-Z]{6}", p.get("friend_code", ""))
        for p in prof.values()
    )
    check(
        "confirm_age makes a profile: generated handle + 6-char friend code, 0 XP",
        ok and all(p.get("xp_total") == 0 and p.get("streak") == 0 for p in prof.values()),
        ", ".join(f"{n}={p.get('handle')}" for n, p in prof.items()),
    )
    r = rpc("confirm_age", {"utc_offset": -240}, UA)
    check(
        "confirm_age again keeps the same handle and code",
        r.ok and r.json()["handle"] == prof["A"]["handle"] and r.json()["friend_code"] == prof["A"]["friend_code"],
    )
    codes = {n: p["friend_code"] for n, p in prof.items()}

    # ------------------------------------------------------------------------------------------ handles
    handle_a = f"Social_{tag}"
    r = rpc("set_handle", {"new_handle": handle_a}, UA)
    check("custom handle accepted (3-20 of A-Z a-z 0-9 _)", r.ok and r.json() == handle_a, short(r))
    r = rpc("set_handle", {"new_handle": handle_a.upper()}, UB)
    check("handles are unique ignoring case", message(r) == "handle_taken", short(r))
    msgs = [message(rpc("set_handle", {"new_handle": h}, UB)) for h in ("ab", "has space", "x" * 21, "émoji_1", "dash-name")]
    check("bad handle shapes rejected", all(m == "handle_invalid" for m in msgs), str(msgs))
    blocked_handles = ("Sh1t_Talker", "BigDick", "f_u_c_k_it", "Admin", "HoopHunter_HQ", "a55hole", "dick69")
    msgs = [message(rpc("set_handle", {"new_handle": h}, UB)) for h in blocked_handles]
    check("blocklist + reserved names rejected (case, leet, underscores)", all(m == "handle_not_allowed" for m in msgs), str(msgs))
    fine = [f"Peacock{tag}", f"Dickens{tag}", f"Therapist{tag}"]
    msgs = [rpc("set_handle", {"new_handle": h}, UB).status_code for h in fine]
    check("innocent words containing blocked ones are allowed", all(s == 200 for s in msgs), str(msgs))
    handle_b = fine[-1]
    handle_c = prof["C"]["handle"]
    handle_d = prof["D"]["handle"]

    # ------------------------------------------------------------------------------------------ RLS / grants
    r = rest("GET", "profiles?select=*", UA)
    check("RLS: a player reads only their own profile row", r.ok and [x["user_id"] for x in r.json()] == [users["A"]["id"]], f"{len(r.json())} row" if r.ok else short(r))
    r = rest("PATCH", f"profiles?user_id=eq.{users['A']['id']}", {**UA, "Prefer": "return=representation"}, json={"streak_days": 999})
    check("player cannot edit their profile directly (streak)", denied(r), short(r))
    r = rest(
        "POST",
        "xp_events",
        UA,
        json={"user_id": users["A"]["id"], "kind": "checkin", "points": 100, "local_day": "2026-10-17", "week_start": "2026-10-12", "dedupe_key": "x"},
    )
    check("player cannot grant themselves XP (insert xp_events)", denied(r), short(r))
    r = rest("POST", "user_badges", UA, json={"user_id": users["A"]["id"], "badge": "streak_30"})
    check("player cannot grant themselves a badge", denied(r), short(r))
    r = rest(
        "POST",
        "checkins",
        UA,
        json={"user_id": users["B"]["id"], "court_id": X[0], "kind": "here", "starts_at": iso(server_now()), "expires_at": iso(server_now()), "local_day": "2026-10-17"},
    )
    check("player cannot insert check-ins directly (or as someone else)", denied(r), short(r))
    bad = [t for t in ("runs", "run_players", "follows", "blocks", "reports", "court_firsts") if not denied(rest("GET", f"{t}?select=*", UA))]
    check("runs, players, follows, blocks, reports, court_firsts are not readable by players", not bad, f"readable: {bad}" if bad else "")
    r = rpc("award_xp", {"p_user": users["A"]["id"], "p_kind": "checkin", "p_points": 50, "p_key": "k", "p_day": "2026-10-17"}, UA)
    r2 = rpc("player", {}, UA)
    check("private helpers (award_xp, ...) are not exposed", r.status_code == 404 and r2.status_code == 404, f"{r.status_code} / {r2.status_code}")

    # ------------------------------------------------------------------------------------------ check-ins
    OFF = -240
    r = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": OFF}, UA)
    j = r.json() if r.ok else {}
    ci = j.get("checkin", {})
    check(
        "here-now check-in: 10 XP, streak 1, first_checkin badge",
        r.ok and j["xp"] == 10 and j["streak"] == 1 and j["badges"] == ["first_checkin"],
        short(r) if not r.ok else str({k: j[k] for k in ("xp", "streak", "badges")}),
    )
    check(
        "here-now expires exactly 2 h after the check-in",
        ci and ts(ci["expires_at"]) - ts(ci["starts_at"]) == timedelta(hours=2),
        f"{ci.get('starts_at')} -> {ci.get('expires_at')}",
    )
    r = rpc("my_profile", {"utc_offset": OFF}, UA)
    check(
        "home state filled from the first check-in's court",
        r.ok and r.json()["home_state"] == state_of[X[0]],
        f"{r.json().get('home_state') if r.ok else short(r)} (court in {state_of[X[0]]})",
    )
    r = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": OFF}, UA)
    check("same court, same day: no more XP", r.ok and r.json()["xp"] == 0, short(r) if not r.ok else f"xp {r.json()['xp']}")
    r = rpc("check_in", {"court": X[1], "mode": "here", "utc_offset": OFF}, UA)
    check("another court the same day: 10 XP", r.ok and r.json()["xp"] == 10, short(r) if not r.ok else f"xp {r.json()['xp']}")
    a0 = rpc("court_activity", {"court": X[0]}, UA).json()
    a1 = rpc("court_activity", {"court": X[1]}, UA).json()
    check(
        "one active check-in: checking in at court 2 ended court 1",
        a0["mine"] is None and a0["here"] == 0 and a1["mine"]["kind"] == "here" and a1["here"] == 1,
        f"court1 {a0}, court2 here={a1['here']}",
    )
    xps = [rpc("check_in", {"court": c, "mode": "here", "utc_offset": OFF}, UA).json()["xp"] for c in X[2:5]]
    check("XP for at most 4 courts a day (5th court earns 0)", xps == [10, 10, 0], str(xps))
    r = rest("GET", "xp_events?select=kind,points,dedupe_key,local_day,week_start", UA)
    ev = r.json() if r.ok else []
    check(
        "XP ledger: 4 check-in awards = 40 XP, and the keys never name a court",
        sum(e["points"] for e in ev if e["kind"] == "checkin") == 40 and not any(c in e["dedupe_key"] for e in ev for c in X),
        str([e["dedupe_key"] for e in ev]),
    )
    check(
        "week_start is the Monday of each XP day",
        ev and all(date.fromisoformat(e["week_start"]) == date.fromisoformat(e["local_day"]) - timedelta(days=date.fromisoformat(e["local_day"]).weekday()) for e in ev),
    )
    r = rest("GET", "checkins?select=*&limit=1", UA)
    cols = set(r.json()[0]) if r.ok and r.json() else set()
    check(
        "check-ins store no coordinates (court id, kind, times only)",
        cols and not cols & {"lat", "lon", "geom", "latitude", "longitude", "location", "accuracy"},
        str(sorted(cols)),
    )

    now = server_now()
    r = rpc("check_in", {"court": X[0], "mode": "planned", "start_at": iso(now + timedelta(hours=1)), "utc_offset": OFF}, UA)
    j = r.json() if r.ok else {}
    ci = j.get("checkin", {})
    check(
        "planned check-in: no XP, expires 2 h after the planned time",
        r.ok and j["xp"] == 0 and ts(ci["expires_at"]) - ts(ci["starts_at"]) == timedelta(hours=2),
        short(r) if not r.ok else f"{ci['starts_at']} -> {ci['expires_at']}",
    )
    b0 = rpc("court_activity", {"court": X[0]}, UB).json()
    check("others see it as planned (count by time, no name)", b0["planned"] and b0["planned"][0]["count"] == 1 and b0["friends"] == [], str(b0))
    today_a = local_day(now, OFF)
    day_after = datetime.combine(today_a + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1) - timedelta(minutes=OFF)
    tomorrow_late = datetime.combine(today_a + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23) - timedelta(minutes=OFF)
    r1 = rpc("check_in", {"court": X[0], "mode": "planned", "start_at": iso(day_after), "utc_offset": OFF}, UA)
    r2 = rpc("check_in", {"court": X[0], "mode": "planned", "start_at": iso(now - timedelta(hours=1)), "utc_offset": OFF}, UA)
    r3 = rpc("check_in", {"court": X[0], "mode": "planned", "start_at": iso(tomorrow_late), "utc_offset": OFF}, UA)
    check(
        "planned time must be today or tomorrow (local), not past",
        message(r1) == "bad_time" and message(r2) == "bad_time" and r3.ok,
        f"day after tomorrow: {message(r1)}, an hour ago: {message(r2)}, tomorrow 11 pm: {r3.status_code}",
    )
    msgs = [message(rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": o}, UA)) for o in (841, -735, 7, None)]
    check("bad UTC offsets rejected (> +14:00, < -12:00, not 15-min steps, missing)", all(m == "bad_offset" for m in msgs), str(msgs))
    r = rpc("check_in", {"court": "osm-way-0-not-a-court", "mode": "here", "utc_offset": OFF}, UA)
    check("unknown court id rejected", message(r) == "court_not_found", short(r))

    uc = f"user-socialtest-{tag}"
    r = rest("POST", "user_courts", UA, json={"id": uc, "user_id": users["A"]["id"], "name": "Social test court", "lon": -85.31, "lat": 35.05})
    user_court_ids.append(uc)
    r1 = rpc("check_in", {"court": uc, "mode": "here", "utc_offset": OFF}, UA)
    r2 = rpc("check_in", {"court": uc, "mode": "here", "utc_offset": OFF}, UB)
    check(
        "check-ins work at your own pending user court; others can't use it",
        r.status_code == 201 and r1.ok and message(r2) == "court_not_found",
        f"{r.status_code} / {r1.status_code} / {message(r2)}",
    )
    rpc("end_checkin", {}, UA)
    r = rpc("my_profile", {"utc_offset": OFF}, UA)
    check("check out: no active check-in left", r.ok and r.json()["checkin"] is None, short(r))

    # ------------------------------------------------------------------------------------------ expiry + purge
    r = rpc("check_in", {"court": X[5], "mode": "here", "utc_offset": OFF}, UB)
    c_before = rpc("court_activity", {"court": X[5]}, UC).json()["here"]
    svc_patch(f"checkins?user_id=eq.{users['B']['id']}&court_id=eq.{X[5]}", {"starts_at": iso(server_now() - timedelta(hours=2, minutes=1))})
    row = rest("GET", f"checkins?select=starts_at,expires_at&user_id=eq.{users['B']['id']}&court_id=eq.{X[5]}", S).json()[0]
    c_after = rpc("court_activity", {"court": X[5]}, UC).json()["here"]
    check(
        "here-now expiry: gone from the court after 2 h (expires_at follows starts_at)",
        c_before == 1 and c_after == 0 and ts(row["expires_at"]) - ts(row["starts_at"]) == timedelta(hours=2),
        f"here {c_before} -> {c_after}",
    )
    r = rpc("check_in", {"court": X[5], "mode": "planned", "start_at": iso(server_now() + timedelta(minutes=30)), "utc_offset": OFF}, UB)
    p_before = len(rpc("court_activity", {"court": X[5]}, UC).json()["planned"])
    svc_patch(f"checkins?user_id=eq.{users['B']['id']}&kind=eq.planned", {"starts_at": iso(server_now() - timedelta(hours=2, minutes=1))})
    p_after = len(rpc("court_activity", {"court": X[5]}, UC).json()["planned"])
    check("planned expiry: gone 2 h after the planned time", r.ok and p_before == 1 and p_after == 0, f"planned {p_before} -> {p_after}")
    svc_patch(f"checkins?user_id=eq.{users['B']['id']}&kind=eq.here", {"starts_at": iso(server_now() - timedelta(days=3))})
    rpc("check_in", {"court": X[6], "mode": "planned", "start_at": iso(server_now() + timedelta(hours=1)), "utc_offset": OFF}, UB)
    left = rest("GET", f"checkins?select=id&user_id=eq.{users['B']['id']}&kind=eq.here", S).json()
    check("check-ins are deleted 2 days after they expire", left == [], f"{len(left)} old rows left")

    # ------------------------------------------------------------------------------------------ runs
    run1 = str(uuid.uuid4())
    start = server_now() + timedelta(hours=2)
    body = {"run": run1, "court": X[0], "run_format": "5v5", "start_at": iso(start), "skill_level": "competitive", "player_limit": 3, "utc_offset": OFF}
    r = rpc("post_run", body, UA)
    j = r.json() if r.ok else {}
    run = j.get("run", {})
    check(
        "post a run: 10 XP, first_run badge, you're in it",
        r.ok and j["xp"] == 10 and j["badges"] == ["first_run"] and run["players"] == 1 and run["names"] == [handle_a] and run["mine"] and run["joined"],
        short(r) if not r.ok else str({k: run.get(k) for k in ("format", "skill", "max_players", "players", "names")}),
    )
    check("runs expire 3 h after the start", run and ts(run["expires_at"]) - ts(run["starts_at"]) == timedelta(hours=3))
    r = rpc("post_run", body, UA)
    check("posting the same run id again is a no-op (offline retry)", r.ok and r.json()["run"]["id"] == run1 and r.json()["xp"] == 0, short(r))
    r = rpc("post_run", {**body, "court": X[1]}, UB)
    check("someone else's run id is refused", message(r) == "bad_run", short(r))
    msgs = [
        message(rpc("post_run", {**body, "run": str(uuid.uuid4()), **change}, UB))
        for change in ({"run_format": "6v6"}, {"skill_level": "pro"}, {"player_limit": 1}, {"start_at": iso(server_now() + timedelta(days=8))})
    ]
    check("presets only: bad format / skill / max players / start rejected", msgs == ["bad_format", "bad_skill", "bad_limit", "bad_time"], str(msgs))
    r = rpc("court_runs", {"court": X[0]}, UB)
    seen = [x for x in r.json() if x["id"] == run1] if r.ok else []
    check("others see the run with the creator's handle", seen and seen[0]["creator"] == handle_a and not seen[0]["mine"] and not seen[0]["joined"], short(r))
    r = rpc("join_run", {"run": run1, "utc_offset": OFF}, UB)
    j = r.json() if r.ok else {}
    check(
        "I'm in: 5 XP, first_run badge, 2 players",
        r.ok and j["xp"] == 5 and j["badges"] == ["first_run"] and j["run"]["players"] == 2 and j["run"]["joined"],
        short(r) if not r.ok else str({"xp": j["xp"], "players": j["run"]["players"]}),
    )
    r1 = rpc("join_run", {"run": run1, "utc_offset": OFF}, UB)
    r2 = rpc("leave_run", {"run": run1}, UB)
    r3 = rpc("join_run", {"run": run1, "utc_offset": OFF}, UB)
    check(
        "join again / leave / rejoin: no extra XP (once per run)",
        r1.ok and r1.json()["xp"] == 0 and r2.ok and r2.json()["players"] == 1 and r3.ok and r3.json()["xp"] == 0 and r3.json()["run"]["players"] == 2,
        f"{short(r1)} / {short(r2)} / {short(r3)}",
    )
    r1 = rpc("join_run", {"run": run1, "utc_offset": OFF}, UC)
    r2 = rpc("join_run", {"run": run1, "utc_offset": OFF}, UD)
    check("max players: the 4th player can't join a 3-player run", r1.ok and message(r2) == "run_full", f"{short(r1)} / {message(r2)}")
    r = rpc("leave_run", {"run": run1}, UA)
    check("the creator can't leave (cancel instead)", message(r) == "creator_cannot_leave", short(r))
    r = rpc("cancel_run", {"run": run1}, UB)
    check("only the creator can cancel", message(r) == "not_your_run", short(r))
    r = rpc("cancel_run", {"run": run1}, UA)
    gone = all(x["id"] != run1 for x in rpc("court_runs", {"court": X[0]}, UB).json())
    r2 = rpc("join_run", {"run": run1, "utc_offset": OFF}, UD)
    check("cancelled: hidden from the court, can't be joined", r.ok and r.json()["cancelled"] and gone and message(r2) == "run_closed", f"{short(r)} / {message(r2)}")

    run2 = str(uuid.uuid4())
    r = rpc("post_run", {**body, "run": run2, "court": X[1], "start_at": iso(server_now() + timedelta(minutes=10)), "player_limit": None}, UA)
    xp2 = r.json()["xp"] if r.ok else None
    svc_patch(f"runs?id=eq.{run2}", {"starts_at": iso(server_now() - timedelta(hours=3, minutes=1))})
    gone = all(x["id"] != run2 for x in rpc("court_runs", {"court": X[1]}, UB).json())
    r2 = rpc("join_run", {"run": run2, "utc_offset": OFF}, UB)
    check("run expiry: gone 3 h after the start, can't be joined", r.ok and gone and message(r2) == "run_closed", message(r2))
    more = [rpc("post_run", {**body, "run": str(uuid.uuid4()), "court": X[2 + i], "player_limit": None}, UA) for i in range(4)]
    check(
        "run XP capped at 2 posts a day; at most 3 upcoming runs per player",
        xp2 == 10 and [m.json()["xp"] for m in more[:3] if m.ok] == [0, 0, 0] and message(more[3]) == "too_many_runs",
        f"xp {xp2}, then {[m.json().get('xp') if m.ok else message(m) for m in more]}",
    )
    run_a_x2 = more[0].json()["run"]["id"]  # A's run at X[2], for the block checks

    # ------------------------------------------------------------------------------------------ reviews + user courts
    r = rest("GET", "courts?select=id&status=eq.candidate&state=eq.TN&order=id&limit=40", A)
    cands = [c["id"] for c in r.json()] if r.ok else []
    counts = rpc("review_counts", {"court_ids": cands}, A).json() if cands else []
    reviewed = {c["court_id"] for c in counts}
    firsts = {c["court_id"] for c in rest("GET", f"court_firsts?select=court_id&court_id=in.({','.join(cands)})", S).json()} if cands else set()
    K = [c for c in cands if c not in reviewed and c not in firsts][:1]
    check("found an unreviewed satellite candidate", bool(K), K[0] if K else "")
    k1 = K[0]

    def review_xp(u):
        r = rest("GET", "xp_events?select=points&kind=eq.review", users[u]["h"])
        return sum(x["points"] for x in r.json()) if r.ok else None

    def badges(u):
        r = rest("GET", "user_badges?select=badge", users[u]["h"])
        return {x["badge"] for x in r.json()} if r.ok else set()

    up = {**UA, "Prefer": "return=representation,resolution=merge-duplicates"}
    r = rest("POST", "reviews?on_conflict=court_id,user_id", up, json={"court_id": k1, "user_id": users["A"]["id"], "decision": "court"})
    first = rest("GET", f"court_firsts?select=user_id,how&court_id=eq.{k1}", S).json()
    check(
        "reviewing a candidate: 5 XP; first to confirm earns Court Hunter",
        r.status_code in (200, 201) and review_xp("A") == 5 and "court_hunter" in badges("A") and first == [{"user_id": users["A"]["id"], "how": "confirmed"}],
        f"{r.status_code}, xp {review_xp('A')}, first {first}",
    )
    rest("POST", "reviews?on_conflict=court_id,user_id", up, json={"court_id": k1, "user_id": users["A"]["id"], "decision": "not_court"})
    rest("DELETE", f"reviews?court_id=eq.{k1}&user_id=eq.{users['A']['id']}", UA)
    r = rest("POST", "reviews?on_conflict=court_id,user_id", up, json={"court_id": k1, "user_id": users["A"]["id"], "decision": "court"})
    check("changing your mind, or undo + redo, earns nothing more", r.status_code in (200, 201) and review_xp("A") == 5, f"xp {review_xp('A')}")
    r = rest("POST", "reviews?on_conflict=court_id,user_id", {**UB, "Prefer": "return=representation,resolution=merge-duplicates"}, json={"court_id": k1, "user_id": users["B"]["id"], "decision": "court"})
    check("the second confirmer gets XP but not Court Hunter", r.status_code in (200, 201) and review_xp("B") == 5 and "court_hunter" not in badges("B"), f"xp {review_xp('B')}")
    r = rest("POST", "reviews?on_conflict=court_id,user_id", up, json={"court_id": "nyc-X159", "user_id": users["A"]["id"], "decision": "court"})
    check("reviewing a verified court earns no XP", r.status_code in (200, 201) and review_xp("A") == 5, f"{r.status_code}, xp {review_xp('A')}")
    rest("DELETE", f"reviews?court_id=eq.nyc-X159&user_id=eq.{users['A']['id']}", UA)

    ucb = f"user-socialtest-{tag}-b"
    r = rest("POST", "user_courts", UB, json={"id": ucb, "user_id": users["B"]["id"], "name": "Social test court B", "lon": -85.30, "lat": 35.04})
    user_court_ids.append(ucb)

    def added_xp(u):
        r = rest("GET", "xp_events?select=points&kind=eq.court_added", users[u]["h"])
        return sum(x["points"] for x in r.json()) if r.ok else None

    pending_xp = added_xp("B")
    svc_patch(f"user_courts?id=eq.{ucb}", {"status": "approved"})
    check(
        "a player-added court earns 25 XP + Court Hunter once approved (not before)",
        r.status_code == 201 and pending_xp == 0 and added_xp("B") == 25 and "court_hunter" in badges("B"),
        f"pending {pending_xp}, approved {added_xp('B')}",
    )
    svc_patch(f"user_courts?id=eq.{ucb}", {"status": "pending"})
    svc_patch(f"user_courts?id=eq.{ucb}", {"status": "approved"})
    check("re-approving earns nothing more", added_xp("B") == 25, f"xp {added_xp('B')}")

    # ------------------------------------------------------------------------------------------ follows
    messy = f" {codes['A'][:3].lower()}-{codes['A'][3:].lower()} "
    r = rpc("follow_by_code", {"code": messy}, UB)
    check("follow by code (any case, spaces and dashes ignored)", r.ok and r.json() == {"status": "followed", "handle": handle_a, "mutual": False}, str(r.json() if r.ok else short(r)))
    r1 = rpc("follow_by_code", {"code": codes["A"]}, UB).json()
    r2 = rpc("follow_by_code", {"code": codes["B"]}, UB).json()
    r3 = rpc("follow_by_code", {"code": "ZZZZZZ" if codes["A"] != "ZZZZZZ" else "YYYYYY"}, UB).json()
    check("already / own code / unknown code", (r1["status"], r2["status"], r3["status"]) == ("already", "own_code", "not_found"), f"{r1} {r2} {r3}")
    r = rpc("follow_by_code", {"code": codes["B"]}, UA)
    check("following back makes it mutual", r.ok and r.json()["mutual"] is True, str(r.json() if r.ok else short(r)))
    rpc("follow_by_code", {"code": codes["B"]}, UC)  # C follows B, not mutual
    f = rpc("my_follows", {}, UA).json()
    check(
        "my_follows: your own following / followers lists",
        f["following"] == [{"handle": handle_b, "mutual": True}] and f["followers"] == [{"handle": handle_b, "mutual": True}] and f["blocked"] == [],
        str(f),
    )
    p = rpc("public_profile", {"player": handle_a}, UC).json()
    check(
        "a public profile is handle + XP / streak / badges only (no lists, ids or code)",
        p and set(p) == {"handle", "me", "xp_week", "xp_total", "streak", "best_streak", "badges", "you_follow", "follows_you"},
        str(sorted(p) if p else p),
    )
    rpc("check_in", {"court": X[7], "mode": "here", "utc_offset": OFF}, UB)
    fa = rpc("court_activity", {"court": X[7]}, UA).json()
    fc = rpc("court_activity", {"court": X[7]}, UC).json()
    check(
        "friends (mutual follows) see your name at a court; others only a count",
        [x["handle"] for x in fa["friends"]] == [handle_b] and fc["friends"] == [] and fc["here"] == 1,
        f"A sees {fa['friends']}, C sees here={fc['here']} friends={fc['friends']}",
    )

    # ------------------------------------------------------------------------------------------ leaderboards
    for n in "ABC":
        rpc("set_home_state", {"st": "tn"}, users[n]["h"])
    week = {n: rpc("my_profile", {"utc_offset": OFF}, users[n]["h"]).json()["xp_week"] for n in "ABC"}
    lb = rpc("leaderboard_friends", {"utc_offset": OFF}, UA).json()
    check(
        "friends leaderboard: you + people you follow, XP this week, ranked",
        [(x["handle"], x["xp"]) for x in lb] == sorted([(handle_a, week["A"]), (handle_b, week["B"])], key=lambda t: -t[1])
        and [x["me"] for x in lb if x["handle"] == handle_a] == [True],
        str(lb),
    )
    lbc = rpc("leaderboard_friends", {"utc_offset": OFF}, UC).json()
    check("C's friends board has B (followed) but not A (not followed)", {x["handle"] for x in lbc} == {handle_c, handle_b}, str(lbc))
    st = rpc("leaderboard_state", {"st": None, "utc_offset": OFF}, UA).json()
    ent = {x["handle"]: x for x in st["entries"]}
    xs = [x["xp"] for x in st["entries"]]
    check(
        "state leaderboard (home state TN): ranked by XP this week, with your rank",
        st["state"] == "TN" and ent.get(handle_a, {}).get("xp") == week["A"] and ent.get(handle_b, {}).get("xp") == week["B"]
        and xs == sorted(xs, reverse=True) and st["me"]["xp"] == week["A"] and st["me"]["rank"] == ent[handle_a]["rank"],
        f"{len(st['entries'])} entries, me {st['me']}, week {st['week_start']}",
    )
    check("week starts on Monday", date.fromisoformat(st["week_start"]).weekday() == 0, st["week_start"])
    r = rpc("leaderboard_state", {"st": "XX", "utc_offset": OFF}, UA)
    check("unknown state rejected", message(r) == "bad_state", short(r))

    # ------------------------------------------------------------------------------------------ blocking
    run_c = str(uuid.uuid4())
    rc = rpc("post_run", {**body, "run": run_c, "court": X[3], "player_limit": None}, UC)
    rpc("join_run", {"run": run_a_x2, "utc_offset": OFF}, UC)  # C joins A's run
    rpc("check_in", {"court": X[3], "mode": "here", "utc_offset": OFF}, UC)
    rpc("follow_by_code", {"code": codes["A"]}, UC)  # C follows A
    before_runs = {x["id"] for x in rpc("court_runs", {"court": X[3]}, UA).json()}
    before_here = rpc("court_activity", {"court": X[3]}, UA).json()["here"]
    r = rpc("block_player", {"player": handle_c}, UA)
    after_runs = {x["id"] for x in rpc("court_runs", {"court": X[3]}, UA).json()}
    after_here = rpc("court_activity", {"court": X[3]}, UA).json()["here"]
    check(
        "block: their runs and check-ins disappear for you",
        rc.ok and r.ok and run_c in before_runs and run_c not in after_runs and before_here == 1 and after_here == 0,
        f"run visible {run_c in before_runs} -> {run_c in after_runs}, here {before_here} -> {after_here}",
    )
    fc_ = rpc("my_follows", {}, UC).json()
    fa_ = rpc("my_follows", {}, UA).json()
    check(
        "block: they no longer follow you",
        handle_a not in [x["handle"] for x in fc_["following"]] and handle_c not in [x["handle"] for x in fa_["followers"]] and fa_["blocked"] == [handle_c],
        f"C follows {fc_['following']}, A blocked {fa_['blocked']}",
    )
    a_run = [x for x in rpc("court_runs", {"court": X[2]}, UB).json() if x["id"] == run_a_x2]
    check("block: they're taken out of the runs you host", a_run and a_run[0]["players"] == 1, str(a_run[0]["players"] if a_run else a_run))
    st_a = {x["handle"] for x in rpc("leaderboard_state", {"st": "TN", "utc_offset": OFF}, UA).json()["entries"]}
    st_c = {x["handle"] for x in rpc("leaderboard_state", {"st": "TN", "utc_offset": OFF}, UC).json()["entries"]}
    check("block: hidden from each other's leaderboards", handle_c not in st_a and handle_a not in st_c and handle_c in st_c, f"A sees C: {handle_c in st_a}, C sees A: {handle_a in st_c}")
    r1 = rpc("follow_by_code", {"code": codes["A"]}, UC).json()
    r2 = rpc("join_run", {"run": run_a_x2, "utc_offset": OFF}, UC)
    r3 = rpc("public_profile", {"player": handle_a}, UC).json()
    r4 = all(x["id"] != run_a_x2 for x in rpc("court_runs", {"court": X[2]}, UC).json())
    check(
        "block works both ways: they can't re-follow, see or join your runs, or open your profile",
        r1["status"] == "not_found" and message(r2) == "run_not_found" and r3 is None and r4,
        f"{r1} / {message(r2)} / {r3} / hidden {r4}",
    )
    r = rpc("block_player", {"player": handle_a}, UA)
    check("you can't block yourself", message(r) == "cannot_block_self", short(r))
    rpc("unblock_player", {"player": handle_c}, UA)
    back = run_c in {x["id"] for x in rpc("court_runs", {"court": X[3]}, UA).json()}
    still = handle_c not in [x["handle"] for x in rpc("my_follows", {}, UA).json()["followers"]]
    check("unblock: their runs show again; the follow isn't restored", back and still, f"run back {back}, follow still gone {still}")
    rpc("unfollow", {"player": handle_b}, UA)
    f = rpc("my_follows", {}, UA).json()
    check("unfollow", f["following"] == [] and f["followers"] == [{"handle": handle_b, "mutual": False}], str(f))

    # ------------------------------------------------------------------------------------------ reports
    r1 = rpc("report_player", {"player": handle_c, "reason": "harassment"}, UA)
    r2 = rpc("report_run", {"run": run_c, "reason": "fake_run"}, UA)
    r3 = rest("POST", "reports", {**UA, "Prefer": "return=minimal"}, json={"run_id": run_c, "reason": "spam"})
    check("report a player / a run (RPC), or insert a report directly", r1.ok and r2.ok and r3.status_code == 201, f"{short(r1)} / {short(r2)} / {short(r3)}")
    r = rest("POST", "reports", {**UA, "Prefer": "return=minimal"}, json={"run_id": run_c, "reason": "spam", "reporter_id": users["B"]["id"]})
    check("can't file a report as someone else", denied(r), short(r))
    r = rest("GET", "reports?select=*", UA)
    r2 = rest("POST", "reports", {**UA, "Prefer": "return=representation"}, json={"run_id": run_c, "reason": "spam"})
    check("reports are insert-only: players can't read them back", denied(r) and denied(r2), f"{short(r)} / {short(r2)}")
    r = rest("POST", "reports", {**UA, "Prefer": "return=minimal"}, json={"run_id": run_c, "reason": "you are bad"})
    r2 = rpc("report_player", {"player": handle_a, "reason": "spam"}, UA)
    check("presets only (no free-text reason); can't report yourself", r.status_code == 400 and message(r2) == "cannot_report_self", f"{short(r)} / {message(r2)}")
    rows = rest("GET", f"reports?select=reporter_id,target_user_id,run_id,reason&reporter_id=eq.{users['A']['id']}", S).json()
    check(
        "the service role reads them: server-set reporter, run reports aimed at the run's creator",
        len(rows) == 3 and all(x["reporter_id"] == users["A"]["id"] and x["target_user_id"] == users["C"]["id"] for x in rows),
        str([x["reason"] for x in rows]),
    )

    # ------------------------------------------------------------------------------------------ streaks + UTC offsets
    # Pick offsets from the server's clock so the player's local time is 23:45-23:59 (late) and, 15 minutes
    # of offset later, 00:00-00:14 the next day (early). Wait if a 15-minute boundary is under 90 s away.
    while True:
        now = server_now()
        mins = now.hour * 60 + now.minute + now.second / 60
        if mins % 15 < 13.5:
            break
        time.sleep(100)
    block = int(mins // 15) * 15
    late = 1425 - block
    while late > 825:
        late -= 1440
    while late < -720:
        late += 1440
    early = late + 15
    d_late, d_early = local_day(now, late), local_day(now, early)
    print(f"[INFO] server {now:%H:%M:%S} UTC; offset {late:+d} min -> local {(now + timedelta(minutes=late)):%H:%M} {d_late}; {early:+d} -> {(now + timedelta(minutes=early)):%H:%M} {d_early}")
    check("boundary offsets straddle local midnight", d_early == d_late + timedelta(days=1), f"{d_late} / {d_early}")

    did = users["D"]["id"]
    svc_patch(f"profiles?user_id=eq.{did}", {"streak_days": 5, "streak_last_day": str(d_late - timedelta(days=1)), "best_streak": 5})
    j1 = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": late}, UD).json()
    j2 = rpc("check_in", {"court": X[1], "mode": "here", "utc_offset": late}, UD).json()
    check(
        "23:5x local: the day after a streak day extends it (5 -> 6); a second court that day doesn't",
        j1["checkin"]["local_day"] == str(d_late) and j1["streak"] == 6 and j2["streak"] == 6 and j1["xp"] == 10 and j2["xp"] == 10,
        f"{j1['checkin']['local_day']} streak {j1['streak']}, {j2['streak']}",
    )
    j3 = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": early}, UD).json()
    j4 = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": early}, UD).json()
    check(
        "00:0x local (minutes later, next local day): streak 7 + streak_7 badge, same court earns XP again",
        j3["checkin"]["local_day"] == str(d_early) and j3["streak"] == 7 and "streak_7" in j3["badges"] and j3["xp"] == 10 and j4["xp"] == 0,
        f"{j3['checkin']['local_day']} streak {j3['streak']} badges {j3['badges']} xp {j3['xp']}/{j4['xp']}",
    )
    j5 = rpc("check_in", {"court": X[2], "mode": "here", "utc_offset": late}, UD).json()
    check("a check-in dated the previous local day never moves the streak back", j5["checkin"]["local_day"] == str(d_late) and j5["streak"] == 7, f"streak {j5['streak']}")
    p = rpc("my_profile", {"utc_offset": early}, UD).json()
    check("profile: streak 7, checked in today", p["streak"] == 7 and p["streak_today"] is True and p["today"] == str(d_early), str({k: p[k] for k in ("streak", "streak_today", "today")}))
    svc_patch(f"profiles?user_id=eq.{did}", {"streak_last_day": str(d_early - timedelta(days=1))})
    p1 = rpc("my_profile", {"utc_offset": early}, UD).json()
    svc_patch(f"profiles?user_id=eq.{did}", {"streak_last_day": str(d_early - timedelta(days=2))})
    p2 = rpc("my_profile", {"utc_offset": early}, UD).json()
    check(
        "a streak stays alive through the next day, then shows 0",
        p1["streak"] == 7 and p1["streak_today"] is False and p2["streak"] == 0,
        f"last = yesterday: {p1['streak']}, two days ago: {p2['streak']}",
    )
    svc_patch(f"profiles?user_id=eq.{did}", {"streak_days": 9, "streak_last_day": str(d_early - timedelta(days=3)), "best_streak": 9})
    j6 = rpc("check_in", {"court": X[3], "mode": "here", "utc_offset": early}, UD).json()
    p = rpc("my_profile", {"utc_offset": early}, UD).json()
    check("a missed day resets the streak to 1; best streak is kept", j6["streak"] == 1 and p["best_streak"] == 9, f"streak {j6['streak']}, best {p['best_streak']}")
    now = server_now()
    j7 = rpc("check_in", {"court": X[4], "mode": "here", "utc_offset": -720}, UD).json()
    j8 = rpc("check_in", {"court": X[5], "mode": "here", "utc_offset": 840}, UD).json()
    exp7, exp8 = local_day(now, -720), local_day(now, 840)
    check(
        "local day at -12:00 and +14:00 matches",
        j7["checkin"]["local_day"] == str(exp7) and j8["checkin"]["local_day"] == str(exp8),
        f"-720: {j7['checkin']['local_day']} (expect {exp7}), +840: {j8['checkin']['local_day']} (expect {exp8})",
    )
    for c in X[6:11]:
        j = rpc("check_in", {"court": c, "mode": "here", "utc_offset": early}, UD).json()
    p = rpc("my_profile", {"utc_offset": early}, UD).json()
    check(
        "10 different courts: courts_10 badge",
        p["courts_visited"] == 10 and "courts_10" in {b["badge"] for b in p["badges"]},
        f"{p['courts_visited']} courts",
    )
    n = 0
    while n < 25:
        r = rpc("check_in", {"court": X[0], "mode": "here", "utc_offset": early}, UD)
        n += 1
        if not r.ok:
            break
    total = len(rest("GET", f"checkins?select=id&user_id=eq.{did}&created_at=gte.{iso(server_now() - timedelta(hours=1))}", S).json())
    check("check-ins are rate limited (20 an hour)", message(r) == "rate_limited" and total == 20, f"{message(r)} after {total}")
    for i in range(20):
        rpc("follow_by_code", {"code": "ZZZZZZ" if codes["A"] != "ZZZZZZ" else "YYYYYY"}, UD)
    r = rpc("follow_by_code", {"code": codes["A"]}, UD).json()
    check("follow_by_code: 20 tries an hour, then rate_limited (even with a right code)", r["status"] == "rate_limited", str(r))

    # ------------------------------------------------------------------------------------------ read isolation
    leaks = []
    for t in ("checkins", "xp_events", "user_badges", "profiles"):
        r = rest("GET", f"{t}?select=user_id", UB)
        if not r.ok or any(x["user_id"] != users["B"]["id"] for x in r.json()):
            leaks.append(t)
    check("RLS: B reads only B's check-ins, XP, badges and profile (none of A's)", not leaks, f"leaks: {leaks}" if leaks else "")
    r = rest("PATCH", f"checkins?user_id=eq.{users['B']['id']}", {**UB, "Prefer": "return=representation"}, json={"xp": True})
    check("player cannot mark a check-in as XP-earning", denied(r), short(r))
finally:
    ids = [u["id"] for u in users.values()]
    for uid in ids:
        r = http.delete(f"{URL}/auth/v1/admin/users/{uid}", headers=S, timeout=30)
        print(f"[INFO] deleted test player: {r.status_code}")
    if ids:
        inlist = ",".join(ids)
        left = {}
        for table, col in (
            ("profiles", "user_id"),
            ("checkins", "user_id"),
            ("runs", "creator_id"),
            ("run_players", "user_id"),
            ("follows", "follower_id"),
            ("blocks", "blocker_id"),
            ("reports", "reporter_id"),
            ("xp_events", "user_id"),
            ("user_badges", "user_id"),
            ("court_firsts", "user_id"),
            ("reviews", "user_id"),
            ("user_courts", "user_id"),
        ):
            r = rest("GET", f"{table}?select={col}&{col}=in.({inlist})", S)
            left[table] = len(r.json()) if r.ok else short(r)
        check("cleanup: test players and all their rows are gone", all(v == 0 for v in left.values()), str({k: v for k, v in left.items() if v}))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
