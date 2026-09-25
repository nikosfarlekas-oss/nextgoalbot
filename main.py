import os
import csv
import json
import time
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask
from scipy.stats import poisson


app = Flask(__name__)


@app.route("/")
def home():
    return "NextGoalBot is alive and running!"


@app.route("/health")
def health():
    return {"status": "ok", "service": "NextGoalBot"}


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FOOTBALL_DATA_API_KEY = os.getenv("API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
FOOTBALL_DATA_URL = "https://api.football-data.org/v4/matches"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

BANKROLL = float(os.getenv("BANKROLL", "100"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MIN_EV_HOME_GOAL = float(os.getenv("MIN_EV_EQUALIZER", "0.035"))
MIN_EV_OVER = float(os.getenv("MIN_EV_OVER", "0.040"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", "0.03"))
HOME_PREMATCH_FAV_MAX_ODD = float(os.getenv("HOME_PREMATCH_FAV_MAX_ODD", "1.70"))

DEFAULT_HOME_XG = float(os.getenv("DEFAULT_HOME_XG", "1.45"))
DEFAULT_AWAY_XG = float(os.getenv("DEFAULT_AWAY_XG", "0.90"))
LEAGUE_AVG_TOTAL_GOALS = float(os.getenv("LEAGUE_AVG_TOTAL_GOALS", "2.60"))

ODDS_EVENTS_CACHE_SECONDS = int(os.getenv("ODDS_EVENTS_CACHE_SECONDS", str(5 * 60)))
TEAM_TOTALS_CACHE_SECONDS = int(os.getenv("TEAM_TOTALS_CACHE_SECONDS", str(5 * 60)))
TEAM_TOTALS_MIN_INTERVAL = float(os.getenv("TEAM_TOTALS_MIN_INTERVAL", "1.0"))

# --- Weekday/weekend-aware daily credit budget -----------------------
#
# The monthly credit allowance is fixed (~500 on the free tier), but
# match volume isn't evenly spread across the week - Sat/Sun have
# far more fixtures than a random Tuesday. Splitting a flat daily
# budget evenly wastes weekday headroom that's never used and
# starves the weekend when it matters most. Defaults below target
# roughly the same monthly total as before (8*5 + 32*2 = 104/week,
# ~450/month - a bit under the ~500 cap as a safety margin), but
# concentrated where the matches actually are. Uses TIMEZONE local
# time so "weekend" matches your actual Saturday/Sunday, not a UTC
# day boundary that could roll over mid-evening.
TIMEZONE = os.getenv("TIMEZONE", "Europe/Athens")
try:
    _tzinfo = ZoneInfo(TIMEZONE)
except Exception as exc:
    print(f"[!] Invalid TIMEZONE '{TIMEZONE}' ({exc}) - falling back to UTC.", flush=True)
    _tzinfo = timezone.utc

ODDS_DAILY_CREDIT_BUDGET_WEEKDAY = int(os.getenv("ODDS_DAILY_CREDIT_BUDGET_WEEKDAY", "8"))
ODDS_DAILY_CREDIT_BUDGET_WEEKEND = int(os.getenv("ODDS_DAILY_CREDIT_BUDGET_WEEKEND", "32"))

CREDIT_COST_EVENTS_CALL = 2
CREDIT_COST_TEAM_TOTALS_CALL = 1
ODDS_429_BACKOFF_SECONDS = int(os.getenv("ODDS_429_BACKOFF_SECONDS", str(6 * 60 * 60)))

# --- Active-hours window for LIVE MATCH POLLING only ------------------
#
# This does NOT replace the opportunity-driven Odds API logic above
# (that stays exactly as it is - it already only spends credits on
# real candidates). This is a separate, orthogonal saving: it stops
# the bot from calling football-data.org and running the whole
# live-match analysis loop at 4am when nothing is on, which reduces
# unnecessary compute time (relevant on hosts like Render's free
# tier) and log noise. Settlement and prematch-cache warming are
# DELIBERATELY exempt from this window - they have their own
# legitimate reasons to run at any hour (settling a match that
# finished after the window closed; warming odds for a fixture that
# kicks off before the window opens).
ACTIVE_HOURS_WEEKDAY = os.getenv("ACTIVE_HOURS_WEEKDAY", "19:00-23:59")
ACTIVE_HOURS_WEEKEND = os.getenv("ACTIVE_HOURS_WEEKEND", "13:00-23:59")
IDLE_CHECK_SECONDS = int(os.getenv("IDLE_CHECK_SECONDS", str(15 * 60)))


def _parse_time_window(window_str):
    try:
        start_str, end_str = window_str.strip().split("-")
        start_h, start_m = (int(part) for part in start_str.split(":"))
        end_h, end_m = (int(part) for part in end_str.split(":"))
        from datetime import time as dtime
        return dtime(start_h, start_m), dtime(end_h, end_m)
    except (ValueError, AttributeError):
        print(
            f"[!] Could not parse active-hours window '{window_str}' - "
            f"expected 'HH:MM-HH:MM'. Treating as always-active.",
            flush=True
        )
        return None


def is_within_active_hours(now_local=None):
    if now_local is None:
        now_local = datetime.now(_tzinfo)
    is_weekend = now_local.weekday() >= 5
    window_str = ACTIVE_HOURS_WEEKEND if is_weekend else ACTIVE_HOURS_WEEKDAY
    window = _parse_time_window(window_str)
    if window is None:
        return True  # fail open: a bad config shouldn't kill the bot
    start, end = window
    current = now_local.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


# How many hours before the active window opens prematch warming is
# allowed to start. Without this, warming runs 24/7 on its own
# PREMATCH_WARM_SECONDS timer regardless of active hours (by design
# - a fixture can kick off before the window opens) - but that also
# meant it could burn the ENTIRE daily credit budget overnight/
# mid-morning on background warming alone, leaving nothing for real
# live alerts once the window actually opens in the evening. This
# caps warming to a sensible band around the window instead of all
# day.
PREMATCH_WARM_LOOKAHEAD_HOURS = float(os.getenv("PREMATCH_WARM_LOOKAHEAD_HOURS", "2"))


def is_within_prematch_warm_period(now_local=None):
    if now_local is None:
        now_local = datetime.now(_tzinfo)
    is_weekend = now_local.weekday() >= 5
    window_str = ACTIVE_HOURS_WEEKEND if is_weekend else ACTIVE_HOURS_WEEKDAY
    window = _parse_time_window(window_str)
    if window is None:
        return True  # fail open

    start, end = window
    today = now_local.date()
    start_dt = datetime.combine(today, start, tzinfo=now_local.tzinfo)
    end_dt = datetime.combine(today, end, tzinfo=now_local.tzinfo)
    if end <= start:
        end_dt += timedelta(days=1)

    warm_start_dt = start_dt - timedelta(hours=PREMATCH_WARM_LOOKAHEAD_HOURS)
    return warm_start_dt <= now_local <= end_dt

# --- Highlightly (live match stats: shots on target, corners, etc.) ---
#
# NOTE ON RELIABILITY: this integration is built from Highlightly's
# public SDK/docs references, NOT a verified live response sample -
# I could not call the real API from here. The base URL and auth
# header below are correct per their official Go client, but the
# exact statistics endpoint path and JSON field names may need a
# small adjustment once you have a key and can inspect a real
# response. See get_live_match_stats_cached() and
# compute_tempo_factor() - both are isolated so a schema tweak only
# touches those two functions, nothing else in the bot.
HIGHLIGHTLY_API_KEY = os.getenv("HIGHLIGHTLY_API_KEY")
HIGHLIGHTLY_BASE_URL = os.getenv("HIGHLIGHTLY_BASE_URL", "https://sports.highlightly.net/football")

# Free tier is 100 requests/day - keep a safety margin under that.
HIGHLIGHTLY_DAILY_REQUEST_BUDGET = int(os.getenv("HIGHLIGHTLY_DAILY_REQUEST_BUDGET", "90"))

# Live stats change fast, but we still don't want to hit the API on
# every single poll cycle for the same match.
HIGHLIGHTLY_STATS_CACHE_SECONDS = int(os.getenv("HIGHLIGHTLY_STATS_CACHE_SECONDS", str(3 * 60)))

# Rough heuristic baseline: combined shots-on-target per 90 minutes
# for an "average pace" match. Used only to scale live xG up/down -
# tune this per league if you like, it's not a precise figure.
LEAGUE_AVG_SOT_PER_90 = float(os.getenv("LEAGUE_AVG_SOT_PER_90", "8.5"))

# Hard clamp so a small-sample noise spike (e.g. 3 shots in the
# first 5 minutes) can't wildly distort the xG estimate.
TEMPO_ADJUSTMENT_MIN = float(os.getenv("TEMPO_ADJUSTMENT_MIN", "0.6"))
TEMPO_ADJUSTMENT_MAX = float(os.getenv("TEMPO_ADJUSTMENT_MAX", "1.6"))

_highlightly_usage = {"date": None, "used": 0}
_highlightly_lock = threading.Lock()
_highlightly_rate_limited_until = 0.0
_highlightly_stats_cache = {}  # key -> {"data": ..., "fetched_at": ...}

PREMATCH_WARM_SECONDS = int(os.getenv("PREMATCH_WARM_SECONDS", str(2 * 60 * 60)))  # 2h - see note above
PREMATCH_CACHE_FILE = os.getenv("PREMATCH_CACHE_FILE", "prematch_cache.json")
_last_prematch_warm = 0.0

SENT_ALERTS_FILE = os.getenv("SENT_ALERTS_FILE", "sent_alerts.json")
ALERTS_LOG_FILE = os.getenv("ALERTS_LOG_FILE", "alerts_log.csv")
PENDING_ALERTS_FILE = os.getenv("PENDING_ALERTS_FILE", "pending_alerts.json")
SETTLEMENT_BUFFER_MINUTES = int(os.getenv("SETTLEMENT_BUFFER_MINUTES", "20"))

ALERTS_LOG_FIELDS = [
    "alert_id", "sent_at", "match_id", "match_name", "market",
    "minute_at_alert", "score_at_alert", "line", "odd",
    "model_probability_pct", "ev_pct", "stake", "bookmaker",
    "outcome", "final_score", "settled_at",
]

sent_alerts = set()
pending_alerts = {}
prematch_cache = {}
_odds_events_cache = {"data": [], "fetched_at": 0.0}
_team_totals_cache = {}
_credit_usage = {"date": None, "used": 0}
_odds_rate_limited_until = 0.0
_last_team_totals_call = 0.0

_alerts_log_lock = threading.Lock()
_pending_alerts_lock = threading.Lock()
_prematch_lock = threading.Lock()
_credit_lock = threading.Lock()
_team_totals_lock = threading.Lock()


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_team_name(name):
    if not name:
        return ""
    value = str(name).lower().strip()
    replacements = [" football club", " fc", " cf", " afc", " sc", " fk", " sk"]
    for replacement in replacements:
        value = value.replace(replacement, "")
    return " ".join(value.split())


def teams_match(home_a, away_a, home_b, away_b):
    ha = normalize_team_name(home_a)
    aa = normalize_team_name(away_a)
    hb = normalize_team_name(home_b)
    ab = normalize_team_name(away_b)
    if not ha or not aa or not hb or not ab:
        return False
    home_match = ha == hb or ha in hb or hb in ha
    away_match = aa == ab or aa in ab or ab in aa
    return home_match and away_match


def poisson_probability_at_least_one(expected_goals):
    expected_goals = max(float(expected_goals), 0.0)
    return 1.0 - poisson.pmf(0, expected_goals)


def calculate_ev(probability, odds):
    if probability is None:
        return None
    if odds is None or odds <= 1:
        return None
    return (probability * odds) - 1.0


def calculate_quarter_kelly(probability, odds):
    if probability is None or odds is None or odds <= 1:
        return 0.0
    p = max(0.0, min(1.0, probability))
    q = 1.0 - p
    b = odds - 1.0
    full_kelly = ((b * p) - q) / b
    if full_kelly <= 0:
        return 0.0
    stake_pct = full_kelly * KELLY_FRACTION
    stake_pct = min(stake_pct, MAX_STAKE_PCT)
    return BANKROLL * stake_pct


def load_sent_alerts():
    global sent_alerts
    if not os.path.exists(SENT_ALERTS_FILE):
        return
    try:
        with open(SENT_ALERTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            sent_alerts = set(str(item) for item in data)
            print(f"[+] Loaded {len(sent_alerts)} sent alerts.", flush=True)
    except (OSError, ValueError) as exc:
        print(f"[-] Could not load sent alerts: {exc}", flush=True)


def save_sent_alerts():
    try:
        with open(SENT_ALERTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted(sent_alerts), fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[-] Could not save sent alerts: {exc}", flush=True)


def load_pending_alerts():
    global pending_alerts
    if not os.path.exists(PENDING_ALERTS_FILE):
        return
    try:
        with open(PENDING_ALERTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            pending_alerts = data
            print(f"[+] Loaded {len(pending_alerts)} pending alerts.", flush=True)
    except (OSError, ValueError) as exc:
        print(f"[-] Could not load pending alerts: {exc}", flush=True)


def save_pending_alerts():
    try:
        with open(PENDING_ALERTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(pending_alerts, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[-] Could not save pending alerts: {exc}", flush=True)


def load_prematch_cache():
    global prematch_cache
    if not os.path.exists(PREMATCH_CACHE_FILE):
        return
    try:
        with open(PREMATCH_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            prematch_cache = data
            print(f"[+] Loaded {len(prematch_cache)} prematch snapshots.", flush=True)
    except (OSError, ValueError) as exc:
        print(f"[-] Could not load prematch cache: {exc}", flush=True)


def save_prematch_cache():
    try:
        with open(PREMATCH_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(prematch_cache, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[-] Could not save prematch cache: {exc}", flush=True)


def ensure_alerts_log_header():
    if os.path.exists(ALERTS_LOG_FILE):
        return
    try:
        with open(ALERTS_LOG_FILE, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=ALERTS_LOG_FIELDS)
            writer.writeheader()
    except OSError as exc:
        print(f"[-] Could not create alerts log: {exc}", flush=True)


def log_alert(alert_id, match_id, match_name, market, minute_at_alert,
              score_at_alert, line, odd, model_probability, ev, stake, bookmaker):
    sent_at = datetime.now(timezone.utc).isoformat()
    row = {
        "alert_id": alert_id, "sent_at": sent_at, "match_id": match_id,
        "match_name": match_name, "market": market,
        "minute_at_alert": minute_at_alert, "score_at_alert": score_at_alert,
        "line": line, "odd": f"{odd:.2f}",
        "model_probability_pct": f"{model_probability * 100:.1f}",
        "ev_pct": f"{ev * 100:.1f}", "stake": f"{stake:.2f}",
        "bookmaker": bookmaker or "", "outcome": "", "final_score": "", "settled_at": "",
    }
    with _alerts_log_lock:
        ensure_alerts_log_header()
        try:
            with open(ALERTS_LOG_FILE, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=ALERTS_LOG_FIELDS)
                writer.writerow(row)
        except OSError as exc:
            print(f"[-] Could not write alert log: {exc}", flush=True)
            return
    with _pending_alerts_lock:
        pending_alerts[str(alert_id)] = {
            "match_id": match_id, "market": market, "line": line,
            "minute_at_alert": minute_at_alert, "sent_at": sent_at,
        }
        save_pending_alerts()


def update_alert_log_outcome(alert_id, outcome, final_score):
    with _alerts_log_lock:
        if not os.path.exists(ALERTS_LOG_FILE):
            return
        try:
            with open(ALERTS_LOG_FILE, "r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
            settled_at = datetime.now(timezone.utc).isoformat()
            for row in rows:
                if row.get("alert_id") == str(alert_id):
                    row["outcome"] = outcome
                    row["final_score"] = final_score
                    row["settled_at"] = settled_at
            with open(ALERTS_LOG_FILE, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=ALERTS_LOG_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
        except (OSError, csv.Error) as exc:
            print(f"[-] Could not update alert log: {exc}", flush=True)


def fetch_match_by_id(match_id):
    if not FOOTBALL_DATA_API_KEY or not match_id:
        return None
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    url = f"{FOOTBALL_DATA_URL}/{match_id}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        print(f"[-] Match lookup HTTP {response.status_code}", flush=True)
    except requests.RequestException as exc:
        print(f"[-] Match lookup error: {exc}", flush=True)
    return None


def fetch_live_matches():
    if not FOOTBALL_DATA_API_KEY:
        print("[!] API_KEY missing.", flush=True)
        return []
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    params = {"status": "IN_PLAY"}
    try:
        response = requests.get(FOOTBALL_DATA_URL, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            try:
                data = response.json()
            except ValueError:
                data = {}
            print(f"[!] football-data {response.status_code}: {data.get('message')}", flush=True)
            return []
        data = response.json()
        matches = data.get("matches", [])
        print(f"[+] Live matches: {len(matches)}", flush=True)
        return matches
    except requests.RequestException as exc:
        print(f"[-] football-data error: {exc}", flush=True)
    except ValueError:
        print("[-] Invalid football-data JSON.", flush=True)
    return []


def calculate_match_minute(match):
    """
    Robust minute resolution:
    - Accepts int/float/numeric-string minute values via safe_int.
    - If no numeric minute is given AND status is PAUSED, does NOT
      assume raw elapsed-since-kickoff (which would keep counting
      through the break) and does NOT blindly assume exactly 45
      either - most PAUSED states really are half-time, but a
      stoppage deep in the second half would be mis-reported as
      45' if we hardcode it. We clamp the elapsed-time estimate to
      a half-time-or-later range instead, which is correct for both
      genuine half-time AND a later in-game stoppage.
    """
    minute_value = match.get("minute")
    parsed_minute = safe_int(minute_value)
    if parsed_minute is not None:
        return max(0, min(parsed_minute, 120))

    status = str(match.get("status", "")).upper()
    utc_date = match.get("utcDate")

    elapsed_minute = None
    if utc_date:
        try:
            start_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            elapsed_minute = max(0, min(int((now - start_time).total_seconds() / 60), 120))
        except ValueError:
            elapsed_minute = None

    if status == "PAUSED":
        if elapsed_minute is not None:
            return max(45, min(elapsed_minute, 105))
        return 45

    return elapsed_minute if elapsed_minute is not None else 0


def extract_match_state(match):
    match_id = match.get("id")
    if not match_id:
        return None
    home_name = match.get("homeTeam", {}).get("name") or "Home"
    away_name = match.get("awayTeam", {}).get("name") or "Away"
    minute = calculate_match_minute(match)
    score = match.get("score", {})
    full_time = score.get("fullTime", {})
    home_goals = safe_int(full_time.get("home"))
    away_goals = safe_int(full_time.get("away"))
    if home_goals is None or away_goals is None:
        print(f"[i] Skipping {home_name} vs {away_name}: missing valid score.", flush=True)
        return None
    return {
        "match": match, "match_id": match_id, "home_name": home_name, "away_name": away_name,
        "match_name": f"{home_name} vs {away_name}", "minute": minute,
        "home_goals": home_goals, "away_goals": away_goals,
        "current_total_goals": home_goals + away_goals,
    }


def odds_api_in_backoff():
    return time.monotonic() < _odds_rate_limited_until


def trigger_odds_backoff():
    global _odds_rate_limited_until
    _odds_rate_limited_until = time.monotonic() + ODDS_429_BACKOFF_SECONDS
    print(f"[!] Odds API 429. Backing off for {ODDS_429_BACKOFF_SECONDS // 60} minutes.", flush=True)


def get_daily_credit_budget():
    now_local = datetime.now(_tzinfo)
    is_weekend = now_local.weekday() >= 5  # Sat=5, Sun=6
    return ODDS_DAILY_CREDIT_BUDGET_WEEKEND if is_weekend else ODDS_DAILY_CREDIT_BUDGET_WEEKDAY


def try_consume_credits(cost):
    with _credit_lock:
        today_local = datetime.now(_tzinfo).date().isoformat()
        if _credit_usage["date"] != today_local:
            _credit_usage["date"] = today_local
            _credit_usage["used"] = 0
        budget = get_daily_credit_budget()
        if _credit_usage["used"] + cost > budget:
            return False
        _credit_usage["used"] += cost
        return True


def odds_api_request(path, params=None):
    if not ODDS_API_KEY:
        print("[!] ODDS_API_KEY missing.", flush=True)
        return None
    if odds_api_in_backoff():
        return None
    request_params = dict(params or {})
    request_params["apiKey"] = ODDS_API_KEY
    url = f"{ODDS_API_BASE}{path}"
    try:
        response = requests.get(url, params=request_params, timeout=10)
        if response.status_code == 429:
            trigger_odds_backoff()
            return None
        if not response.ok:
            print(f"[-] Odds API {response.status_code}: {response.text[:300]}", flush=True)
            return None
        return response.json()
    except requests.RequestException as exc:
        print(f"[-] Odds API request error: {exc}", flush=True)
    except ValueError:
        print("[-] Invalid JSON from Odds API.", flush=True)
    return None


def get_soccer_events():
    data = odds_api_request("/sports/upcoming/odds", {
        "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal",
    })
    if not isinstance(data, list):
        return None
    return [event for event in data if str(event.get("sport_key", "")).startswith("soccer_")]


def get_soccer_events_cached():
    now = time.monotonic()
    cache_age = now - _odds_events_cache["fetched_at"]
    if _odds_events_cache["fetched_at"] > 0 and cache_age < ODDS_EVENTS_CACHE_SECONDS:
        return _odds_events_cache["data"]
    if not try_consume_credits(CREDIT_COST_EVENTS_CALL):
        print("[!] Daily Odds API budget reached. Using cached events.", flush=True)
        return _odds_events_cache["data"]
    data = get_soccer_events()
    if data is not None:
        _odds_events_cache["data"] = data
        _odds_events_cache["fetched_at"] = now
    return _odds_events_cache["data"]


def find_odds_event(home_team, away_team, events):
    for event in events:
        if teams_match(home_team, away_team, event.get("home_team"), event.get("away_team")):
            return event
    return None


def extract_h2h(event):
    bookmakers = event.get("bookmakers", [])
    home_name = event.get("home_team")
    away_name = event.get("away_team")
    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            prices = {}
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = safe_float(outcome.get("price"))
                if name and price and price > 1:
                    prices[name] = price
            if home_name in prices and away_name in prices and "Draw" in prices:
                return {
                    "home": prices[home_name], "away": prices[away_name],
                    "draw": prices["Draw"], "bookmaker": bookmaker.get("title"),
                }
    return None


def estimate_xg_from_odds(home_odd, away_odd, draw_odd):
    """
    HEURISTIC estimate, NOT a real calibrated xG model. Derives a
    rough expected-goals split from de-vigged pre-match 1X2 implied
    probabilities and a league-average total-goals assumption. It
    ignores in-game context entirely (current score, red cards,
    tactical changes, injuries, etc.) - a better-than-nothing proxy
    when no real xG source is configured, not a substitute for one.
    """
    if home_odd is None or away_odd is None or home_odd <= 1 or away_odd <= 1:
        return None
    implied_home = 1.0 / home_odd
    implied_away = 1.0 / away_odd
    implied_draw = 1.0 / draw_odd if draw_odd and draw_odd > 1 else 0.0
    overround = implied_home + implied_away + implied_draw
    if overround <= 0:
        return None
    p_home = implied_home / overround
    p_away = implied_away / overround
    p_home = max(p_home, 0.05)
    p_away = max(p_away, 0.05)
    share_home = p_home / (p_home + p_away)
    share_away = 1.0 - share_home
    home_xg = LEAGUE_AVG_TOTAL_GOALS * share_home
    away_xg = LEAGUE_AVG_TOTAL_GOALS * share_away
    return (home_xg, away_xg)


def get_match_xg(match, event_id):
    explicit_home = safe_float(match.get("home_xg"))
    explicit_away = safe_float(match.get("away_xg"))
    if explicit_home is not None and explicit_away is not None:
        return (explicit_home, explicit_away)
    prematch = None
    if event_id:
        with _prematch_lock:
            prematch = prematch_cache.get(event_id)
    if prematch:
        estimated = estimate_xg_from_odds(prematch.get("home_odd"), prematch.get("away_odd"), prematch.get("draw_odd"))
        if estimated:
            return estimated
    return (DEFAULT_HOME_XG, DEFAULT_AWAY_XG)


def update_prematch_cache(events):
    if not events:
        return
    now = datetime.now(timezone.utc)
    changed = False
    with _prematch_lock:
        for event in events:
            event_id = event.get("id")
            commence = event.get("commence_time")
            if not event_id or not commence:
                continue
            try:
                kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            except ValueError:
                continue
            if kickoff <= now:
                continue
            h2h = extract_h2h(event)
            if not h2h:
                continue
            new_snapshot = {
                "home": event.get("home_team"), "away": event.get("away_team"),
                "home_odd": h2h["home"], "away_odd": h2h["away"],
                "draw_odd": h2h["draw"], "bookmaker": h2h["bookmaker"],
                "kickoff": commence,
            }
            previous = prematch_cache.get(event_id)
            # Compare without a volatile timestamp field so we don't
            # treat "same odds, refreshed again" as a real change and
            # write to disk every single cycle for no reason.
            if previous is None or {k: v for k, v in previous.items() if k != "captured_at"} != new_snapshot:
                new_snapshot["captured_at"] = now.isoformat()
                prematch_cache[event_id] = new_snapshot
                changed = True
    if changed:
        save_prematch_cache()


def warm_prematch_cache_if_due():
    global _last_prematch_warm

    if not is_within_prematch_warm_period():
        return

    now = time.monotonic()
    if now - _last_prematch_warm < PREMATCH_WARM_SECONDS:
        return
    _last_prematch_warm = now
    events = get_soccer_events_cached()
    if events:
        update_prematch_cache(events)


def get_current_over_odds(event, current_total_goals):
    target_point = float(current_total_goals) + 0.5
    best = None
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "totals":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                point = safe_float(outcome.get("point"))
                price = safe_float(outcome.get("price"))
                if not (name == "Over" and point is not None and price is not None and abs(point - target_point) < 0.001):
                    continue
                if best is None or price > best["odd"]:
                    best = {"odd": price, "point": point, "bookmaker": bookmaker.get("title")}
    return best


def get_team_total_odds(event_id, sport_key, team_name, target_point):
    if not event_id or not sport_key or target_point is None:
        return None
    global _last_team_totals_call
    with _team_totals_lock:
        elapsed = time.monotonic() - _last_team_totals_call
        if elapsed < TEAM_TOTALS_MIN_INTERVAL:
            time.sleep(TEAM_TOTALS_MIN_INTERVAL - elapsed)
        _last_team_totals_call = time.monotonic()
    data = odds_api_request(f"/sports/{sport_key}/events/{event_id}/odds", {
        "regions": "eu", "markets": "team_totals", "oddsFormat": "decimal",
    })
    if not isinstance(data, dict):
        return None
    best = None
    for bookmaker in data.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "team_totals":
                continue
            for outcome in market.get("outcomes", []):
                description = outcome.get("description")
                name = outcome.get("name")
                point = safe_float(outcome.get("point"))
                price = safe_float(outcome.get("price"))
                if not (description and normalize_team_name(description) == normalize_team_name(team_name)
                        and name == "Over" and point is not None and price is not None
                        and abs(point - target_point) < 0.001):
                    continue
                if best is None or price > best["odd"]:
                    best = {"odd": price, "point": point, "bookmaker": bookmaker.get("title")}
    return best


def get_team_total_odds_cached(event_id, sport_key, team_name, target_point):
    if not event_id or not sport_key or target_point is None:
        return None
    key = f"{event_id}:{normalize_team_name(team_name)}:{target_point:.1f}"
    now = time.monotonic()
    cached = _team_totals_cache.get(key)
    if cached and (now - cached["fetched_at"]) < TEAM_TOTALS_CACHE_SECONDS:
        return cached["data"]
    if not try_consume_credits(CREDIT_COST_TEAM_TOTALS_CALL):
        print("[!] Daily Odds API budget reached - skipping team_totals.", flush=True)
        return cached["data"] if cached else None
    data = get_team_total_odds(event_id, sport_key, team_name, target_point)
    _team_totals_cache[key] = {"data": data, "fetched_at": now}
    return data


# =========================================================
# HIGHLIGHTLY - LIVE MATCH STATS (tempo adjustment for Over)
# =========================================================

def try_consume_highlightly_request(cost=1):
    with _highlightly_lock:
        today = datetime.now(timezone.utc).date().isoformat()
        if _highlightly_usage["date"] != today:
            _highlightly_usage["date"] = today
            _highlightly_usage["used"] = 0
        if _highlightly_usage["used"] + cost > HIGHLIGHTLY_DAILY_REQUEST_BUDGET:
            return False
        _highlightly_usage["used"] += cost
        return True


def highlightly_api_request(path, params=None):
    global _highlightly_rate_limited_until
    if not HIGHLIGHTLY_API_KEY:
        return None
    if time.monotonic() < _highlightly_rate_limited_until:
        return None
    headers = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY}
    url = f"{HIGHLIGHTLY_BASE_URL}{path}"
    try:
        response = requests.get(url, headers=headers, params=params or {}, timeout=10)
        if response.status_code == 429:
            _highlightly_rate_limited_until = time.monotonic() + 6 * 60 * 60
            print("[!] Highlightly 429 - backing off 6h.", flush=True)
            return None
        if not response.ok:
            print(f"[-] Highlightly API {response.status_code}: {response.text[:300]}", flush=True)
            return None
        return response.json()
    except requests.RequestException as exc:
        print(f"[-] Highlightly request error: {exc}", flush=True)
    except ValueError:
        print("[-] Invalid JSON from Highlightly.", flush=True)
    return None


def find_highlightly_match_id(home_name, away_name):
    """
    NOTE: the /matches query parameters (date filter, live-only
    filter, response shape) are NOT verified against a live
    response - adjust the params/parsing below once you can inspect
    the real payload against https://highlightly.net/documentation/football/.
    """
    data = highlightly_api_request("/matches", {
        "date": datetime.now(timezone.utc).date().isoformat(),
    })
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        return None
    for entry in data:
        home = (
            entry.get("homeTeam", {}).get("name")
            if isinstance(entry.get("homeTeam"), dict)
            else entry.get("home_team")
        )
        away = (
            entry.get("awayTeam", {}).get("name")
            if isinstance(entry.get("awayTeam"), dict)
            else entry.get("away_team")
        )
        if teams_match(home_name, away_name, home, away):
            return entry.get("id")
    return None


def get_live_match_stats_cached(home_name, away_name):
    """
    Cached, budgeted fetch of live match statistics for a fixture,
    matched by team name. Returns whatever the raw API gives back
    (shape NOT fully verified - see compute_tempo_factor() for how
    it's consumed defensively).
    """
    if not HIGHLIGHTLY_API_KEY:
        return None

    key = f"{normalize_team_name(home_name)}:{normalize_team_name(away_name)}"
    now = time.monotonic()
    cached = _highlightly_stats_cache.get(key)
    if cached and (now - cached["fetched_at"]) < HIGHLIGHTLY_STATS_CACHE_SECONDS:
        return cached["data"]

    if not try_consume_highlightly_request(1):
        print("[!] Highlightly daily budget reached - skipping live stats.", flush=True)
        return cached["data"] if cached else None

    match_id = find_highlightly_match_id(home_name, away_name)
    if not match_id:
        _highlightly_stats_cache[key] = {"data": None, "fetched_at": now}
        return None

    if not try_consume_highlightly_request(1):
        _highlightly_stats_cache[key] = {"data": None, "fetched_at": now}
        return None

    # NOTE: endpoint path/shape not verified live - adjust here if needed.
    stats = highlightly_api_request(f"/matches/{match_id}/statistics")
    _highlightly_stats_cache[key] = {"data": stats, "fetched_at": now}
    return stats


def compute_tempo_factor(stats, minute):
    """
    Rough heuristic: compares combined shots-on-target so far to a
    league-average pace for this point in the match, to scale the
    remaining-time xG up (busier than average game) or down
    (quieter than average game). Returns 1.0 (neutral, no
    adjustment) whenever the stats are missing, malformed, or the
    expected fields simply aren't present - tempo adjustment should
    never be able to crash the alert logic OR mistake "field not
    present" for "confirmed zero shots" (a real quiet 0-0 game and
    a broken/incomplete API response must not be treated the same).
    """
    if not isinstance(stats, dict) or minute <= 0:
        return 1.0

    home_block = stats.get("home") or stats.get("homeTeam")
    away_block = stats.get("away") or stats.get("awayTeam")
    if not isinstance(home_block, dict) or not isinstance(away_block, dict):
        return 1.0

    home_present = "shots_on_target" in home_block or "shotsOnTarget" in home_block
    away_present = "shots_on_target" in away_block or "shotsOnTarget" in away_block
    if not (home_present and away_present):
        return 1.0

    home_sot = safe_float(home_block.get("shots_on_target", home_block.get("shotsOnTarget"))) or 0.0
    away_sot = safe_float(away_block.get("shots_on_target", away_block.get("shotsOnTarget"))) or 0.0

    combined_sot = home_sot + away_sot
    expected_sot_by_now = LEAGUE_AVG_SOT_PER_90 * (minute / 90.0)
    if expected_sot_by_now <= 0:
        return 1.0

    factor = combined_sot / expected_sot_by_now
    return max(TEMPO_ADJUSTMENT_MIN, min(TEMPO_ADJUSTMENT_MAX, factor))


def state_has_opportunity(state):
    minute = state["minute"]
    over_window = 20 <= minute <= 82
    home_goal_window = 10 <= minute <= 78 and state["away_goals"] == state["home_goals"] + 1
    return over_window or home_goal_window


def find_prematch_snapshot_by_teams(home_name, away_name):
    """
    Cheap, no-API-call lookup: searches the already-cached
    prematch_cache for a snapshot matching these team names, using
    the same fuzzy matching find_odds_event() uses against a live
    events list. This lets us pre-check the hard favourite
    condition for the home-goal branch WITHOUT first spending an
    Odds API call on an events listing - a trailing-by-one match
    whose home side was never a confirmed pre-match favourite can
    be ruled out for free, before it ever counts as a "real"
    opportunity worth calling the Odds API for.
    """
    with _prematch_lock:
        snapshots = list(prematch_cache.values())
    for snapshot in snapshots:
        if teams_match(home_name, away_name, snapshot.get("home"), snapshot.get("away")):
            return snapshot
    return None


def is_confirmed_prematch_favourite(snapshot):
    if not snapshot:
        return False
    home_pre = safe_float(snapshot.get("home_odd"))
    away_pre = safe_float(snapshot.get("away_odd"))
    return (
        home_pre is not None
        and away_pre is not None
        and home_pre <= HOME_PREMATCH_FAV_MAX_ODD
        and home_pre < away_pre
    )


def process_single_match(state, odds_events):
    alert_sent = False
    match_id = state["match_id"]
    home_name = state["home_name"]
    away_name = state["away_name"]
    match_name = state["match_name"]
    minute = state["minute"]
    home_goals = state["home_goals"]
    away_goals = state["away_goals"]
    current_total_goals = state["current_total_goals"]
    match = state["match"]

    odds_event = find_odds_event(home_name, away_name, odds_events)
    if not odds_event:
        return False

    odds_event_id = odds_event.get("id")
    sport_key = odds_event.get("sport_key")
    home_xg, away_xg = get_match_xg(match, odds_event_id)
    remaining_minutes = max(90 - minute, 1)
    time_remaining = remaining_minutes / 90.0

    if 20 <= minute <= 82:
        alert_key = f"{match_id}_over_{current_total_goals}"
        if alert_key not in sent_alerts:
            over_data = get_current_over_odds(odds_event, current_total_goals)
            if over_data:
                live_odd = over_data["odd"]
                target_line = over_data["point"]

                # Live tempo adjustment (optional - only if a
                # Highlightly key is configured). Scales the base
                # xG up/down based on actual shots-on-target so far
                # vs a league-average pace, instead of relying
                # purely on the static pre-match xG estimate.
                tempo_factor = 1.0
                if HIGHLIGHTLY_API_KEY:
                    live_stats = get_live_match_stats_cached(home_name, away_name)
                    tempo_factor = compute_tempo_factor(live_stats, minute)

                total_xg = (home_xg + away_xg) * time_remaining * tempo_factor
                prob_over = poisson_probability_at_least_one(total_xg)
                ev_over = calculate_ev(prob_over, live_odd)
                if ev_over is not None and ev_over >= MIN_EV_OVER and (home_xg + away_xg) >= 1.20:
                    stake = calculate_quarter_kelly(prob_over, live_odd)
                    tempo_line = (
                        f"🏃 Tempo: {tempo_factor:.2f}x\n" if HIGHLIGHTLY_API_KEY else ""
                    )
                    msg = (
                        "🚨 VALUE BET ALERT 🚨\n\n"
                        f"⚽ Αγώνας: {match_name}\n"
                        f"📊 Σκορ: {home_goals}-{away_goals} ({minute}')\n"
                        f"🎯 Market: Over {target_line:.1f}\n"
                        f"📈 Live Odd: {live_odd:.2f}\n"
                        f"🧮 Model probability: {prob_over * 100:.1f}%\n"
                        f"{tempo_line}"
                        f"💡 EV: +{ev_over * 100:.1f}%\n"
                        f"💵 Quarter-Kelly stake: {stake:.2f}€\n"
                        f"🏦 Bookmaker: {over_data['bookmaker']}"
                    )
                    if send_telegram_alert(msg):
                        sent_alerts.add(alert_key)
                        save_sent_alerts()
                        log_alert(
                            alert_id=alert_key, match_id=match_id, match_name=match_name,
                            market="over", minute_at_alert=minute,
                            score_at_alert=f"{home_goals}-{away_goals}", line=target_line,
                            odd=live_odd, model_probability=prob_over, ev=ev_over,
                            stake=stake, bookmaker=over_data["bookmaker"],
                        )
                        alert_sent = True

    # Equalizer: home must be trailing by EXACTLY one goal.
    if 10 <= minute <= 78 and away_goals == home_goals + 1:
        alert_key = f"{match_id}_home_goal"
        if alert_key not in sent_alerts:

            with _prematch_lock:
                prematch = prematch_cache.get(odds_event_id)

            if not prematch:
                print(f"[i] Skipping home goal alert for {match_name}: no pre-match odds snapshot.", flush=True)
                return alert_sent

            home_pre = safe_float(prematch.get("home_odd"))
            away_pre = safe_float(prematch.get("away_odd"))

            if (home_pre is None or away_pre is None
                    or home_pre > HOME_PREMATCH_FAV_MAX_ODD or home_pre >= away_pre):
                print(
                    f"[i] Skipping home goal alert for {match_name}: home not confirmed "
                    f"favourite <= {HOME_PREMATCH_FAV_MAX_ODD:.2f} (home={home_pre}, away={away_pre}).",
                    flush=True
                )
                return alert_sent

            target_point = home_goals + 0.5
            team_total = get_team_total_odds_cached(
                event_id=odds_event_id, sport_key=sport_key,
                team_name=home_name, target_point=target_point,
            )

            if team_total:
                live_odd = team_total["odd"]
                team_total_line = team_total["point"]
                remaining_home_xg = home_xg * time_remaining
                prob_home_goal = poisson_probability_at_least_one(remaining_home_xg)
                ev_home_goal = calculate_ev(prob_home_goal, live_odd)

                if ev_home_goal is not None and ev_home_goal >= MIN_EV_HOME_GOAL:
                    stake = calculate_quarter_kelly(prob_home_goal, live_odd)
                    msg = (
                        "🔥 HOME TEAM GOAL VALUE 🔥\n\n"
                        f"⚽ Αγώνας: {match_name}\n"
                        f"📊 Σκορ: {home_goals}-{away_goals} ({minute}')\n"
                        f"🎯 Market: {home_name} Team Total Over {team_total_line:.1f}\n"
                        f"📈 Live Odd: {live_odd:.2f}\n"
                        f"🧮 Model probability: {prob_home_goal * 100:.1f}%\n"
                        f"💡 EV: +{ev_home_goal * 100:.1f}%\n"
                        f"💵 Quarter-Kelly stake: {stake:.2f}€\n"
                        f"⭐ Pre-match favourite: {home_name} @{home_pre:.2f}\n"
                        f"🏦 Bookmaker: {team_total['bookmaker']}"
                    )
                    if send_telegram_alert(msg):
                        sent_alerts.add(alert_key)
                        save_sent_alerts()
                        log_alert(
                            alert_id=alert_key, match_id=match_id, match_name=match_name,
                            market="home_team_total", minute_at_alert=minute,
                            score_at_alert=f"{home_goals}-{away_goals}", line=team_total_line,
                            odd=live_odd, model_probability=prob_home_goal, ev=ev_home_goal,
                            stake=stake, bookmaker=team_total["bookmaker"],
                        )
                        alert_sent = True

    return alert_sent


def analyze_matches(live_matches):
    global sent_alerts
    if not live_matches:
        return

    match_states = []
    for match in live_matches:
        state = extract_match_state(match)
        if state is not None:
            match_states.append(state)

    if not match_states:
        print("[i] No valid match states.", flush=True)
        return

    # Over-window matches are always worth checking - the Over
    # market has no pre-condition beyond being in the minute window.
    over_candidates = [
        state for state in match_states if 20 <= state["minute"] <= 82
    ]

    # Home-goal-window matches (trailing by exactly one) are common
    # - most of them will NOT have had a confirmed pre-match
    # favourite, so we rule those out here using only the already-
    # cached prematch_cache (zero API cost) BEFORE deciding this
    # cycle is worth spending an Odds API call on at all. Only
    # matches that pass this cheap pre-check count as real
    # candidates below.
    home_goal_raw = [
        state for state in match_states
        if 10 <= state["minute"] <= 78 and state["away_goals"] == state["home_goals"] + 1
    ]
    home_goal_candidates = [
        state for state in home_goal_raw
        if is_confirmed_prematch_favourite(
            find_prematch_snapshot_by_teams(state["home_name"], state["away_name"])
        )
    ]
    skipped_non_favourites = len(home_goal_raw) - len(home_goal_candidates)
    if skipped_non_favourites:
        print(
            f"[i] {skipped_non_favourites} trailing-by-1 match(es) skipped "
            "for free - home wasn't a confirmed pre-match favourite.",
            flush=True
        )

    candidates = over_candidates + home_goal_candidates
    if not candidates:
        print(f"[i] {len(match_states)} live match(es), no alert window.", flush=True)
        return

    print(f"[i] {len(candidates)}/{len(match_states)} matches inside alert window.", flush=True)
    odds_events = get_soccer_events_cached()
    if odds_events:
        update_prematch_cache(odds_events)

    alerts_sent_this_cycle = False
    for state in candidates:
        try:
            sent = process_single_match(state, odds_events)
            if sent:
                alerts_sent_this_cycle = True
        except Exception as exc:
            print(f"[-] Match analysis error: {exc}", flush=True)

    if alerts_sent_this_cycle:
        save_sent_alerts()

    with _credit_lock:
        print(f"[i] Odds API credits today: {_credit_usage['used']}/{get_daily_credit_budget()}", flush=True)


def send_telegram_alert(message):
    if not TELEGRAM_TOKEN:
        print("[!] TELEGRAM_BOT_TOKEN missing.", flush=True)
        return False
    if not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_CHAT_ID missing.", flush=True)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.ok:
            print("[+] Telegram alert sent.", flush=True)
            return True
        print(f"[-] Telegram error {response.status_code}: {response.text[:300]}", flush=True)
    except requests.RequestException as exc:
        print(f"[-] Telegram request error: {exc}", flush=True)
    return False


def settle_pending_alerts():
    if not pending_alerts:
        return
    now = datetime.now(timezone.utc)
    sent_alerts_changed = False

    for alert_id, info in list(pending_alerts.items()):
        try:
            sent_at = datetime.fromisoformat(info["sent_at"])
        except (KeyError, ValueError):
            with _pending_alerts_lock:
                pending_alerts.pop(alert_id, None)
            continue

        alert_minute = safe_int(info.get("minute_at_alert", 90), 90)
        remaining_minutes = max(0, 90 - alert_minute)
        not_before = sent_at + timedelta(minutes=(remaining_minutes + SETTLEMENT_BUFFER_MINUTES))
        if now < not_before:
            continue

        match_data = fetch_match_by_id(info.get("match_id"))
        if not match_data:
            continue

        status = str(match_data.get("status", "")).upper()

        if status != "FINISHED":
            if now > sent_at + timedelta(hours=6):
                print(f"[!] Settlement UNKNOWN for {alert_id} (status={status}).", flush=True)
                update_alert_log_outcome(alert_id, "UNKNOWN", f"STATUS:{status}")
                with _pending_alerts_lock:
                    pending_alerts.pop(alert_id, None)
                if alert_id in sent_alerts:
                    sent_alerts.discard(alert_id)
                    sent_alerts_changed = True
            continue

        score = match_data.get("score", {}).get("fullTime", {})
        home_goals = safe_int(score.get("home"))
        away_goals = safe_int(score.get("away"))
        if home_goals is None or away_goals is None:
            continue

        final_score = f"{home_goals}-{away_goals}"
        line = safe_float(info.get("line"))
        market = info.get("market")

        if line is None:
            outcome = "UNKNOWN"
        elif market == "over":
            total_goals = home_goals + away_goals
            outcome = "WIN" if total_goals > line else "LOSE"
        elif market == "home_team_total":
            outcome = "WIN" if home_goals > line else "LOSE"
        else:
            outcome = "UNKNOWN"

        update_alert_log_outcome(alert_id, outcome, final_score)
        print(f"[i] Settled {alert_id}: {outcome} (final {final_score}, line {line})", flush=True)

        with _pending_alerts_lock:
            pending_alerts.pop(alert_id, None)
        if alert_id in sent_alerts:
            sent_alerts.discard(alert_id)
            sent_alerts_changed = True

    save_pending_alerts()
    if sent_alerts_changed:
        save_sent_alerts()


def run_bot():
    print("[*] NextGoalBot started.", flush=True)
    print(f"[*] football-data polling: every {POLL_SECONDS}s", flush=True)
    print(f"[*] Active hours - weekdays: {ACTIVE_HOURS_WEEKDAY}, weekends: {ACTIVE_HOURS_WEEKEND} ({TIMEZONE})", flush=True)
    print("[*] Odds API: opportunity-driven.", flush=True)
    print(f"[*] HARD home pre-match favourite: <= {HOME_PREMATCH_FAV_MAX_ODD:.2f}", flush=True)
    print("[*] Equalizer condition: home is exactly 1 goal behind.", flush=True)
    print("[*] Home Team Total target: current home goals + 0.5", flush=True)

    load_sent_alerts()
    load_pending_alerts()
    load_prematch_cache()

    try:
        warm_prematch_cache_if_due()
    except Exception as exc:
        print(f"[-] Initial prematch warm error: {exc}", flush=True)

    was_active = None  # tracks transitions, so we only log on change

    while True:
        active = is_within_active_hours()
        if active != was_active:
            print(
                "[*] Entering active window - resuming live match polling." if active
                else "[*] Outside active window - pausing live match polling "
                     "(settlement and prematch warming keep running).",
                flush=True
            )
            was_active = active

        try:
            if active:
                live_matches = fetch_live_matches()
                if live_matches:
                    analyze_matches(live_matches)

            # These run regardless of the active window - a match
            # that finished after the window closed still needs
            # settling, and a fixture that kicks off before the
            # window opens still needs its prematch odds captured.
            try:
                settle_pending_alerts()
            except Exception as exc:
                print(f"[-] Settlement error: {exc}", flush=True)

            try:
                warm_prematch_cache_if_due()
            except Exception as exc:
                print(f"[-] Prematch cache error: {exc}", flush=True)

        except Exception as exc:
            print(f"[-] Main loop error: {exc}", flush=True)

        time.sleep(POLL_SECONDS if active else IDLE_CHECK_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
