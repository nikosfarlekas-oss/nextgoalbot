import math
import requests
from scipy.stats import poisson
import time
import os
import threading
from flask import Flask
from football_ou_bot import analyze_over_under
from datetime import datetime, timezone

app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive and running!"


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


# ==========================================
# 1. ΡΥΘΜΙΣΕΙΣ & ENV VARIABLES
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8737186847:AAFNhoe3_dhZdig9IjjC3ttGa_yB44Eqdrg")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8819668615")
API_KEY = os.getenv("API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_URL = "https://api.football-data.org/v4/matches"
sent_alerts = set()  # Λίστα για να μην στέλνει διπλά alerts


def send_telegram_alert(message):
    if not TELEGRAM_TOKEN:
        print("[!] Προειδοποίηση: Δεν έχεις βάλει το Telegram Token!", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
        print("[+] Το alert στάλθηκε επιτυχώς στο Telegram!", flush=True)
    except Exception as e:
        print(f"[-] Σφάλμα κατά τη αποστολή στο Telegram: {e}", flush=True)


# ==========================================
# 2. ΗΒΡΙΔΙΚΗ ΛΗΨΗ LIVE ODDS (THE ODDS API)
# ==========================================
def get_live_odds_from_odds_api(home_team, away_team):
    """Τραβάει τις πραγματικές αποδόσεις 1X2 και Over/Under με έξυπνη σύγκριση ονομάτων."""
    if not ODDS_API_KEY:
        return None

    try:
        url = f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h,totals&live=true"
        res = requests.get(url, timeout=5)

        if res.status_code == 200:
            events = res.json()

            # Καθαρισμός ονομάτων για καλύτερο matching
            h_clean = home_team.lower().replace("fc", "").replace("stade", "").strip()
            a_clean = away_team.lower().replace("fc", "").replace("stade", "").strip()

            for event in events:
                event_h = event.get("home_team", "").lower()
                event_a = event.get("away_team", "").lower()

                h_words = [w for w in h_clean.split() if len(w) > 3]
                match_found = any(w in event_h for w in h_words) if h_words else (h_clean in event_h)

                if match_found:
                    bookmakers = event.get("bookmakers", [])
                    if bookmakers:
                        markets = bookmakers[0].get("markets", [])
                        home_win_odd = None
                        away_win_odd = None
                        live_over_odd = None

                        for m in markets:
                            if m.get("key") == "h2h":
                                outcomes = m.get("outcomes", [])
                                for out in outcomes:
                                    if out.get("name") == event.get("home_team"):
                                        home_win_odd = float(out.get("price"))
                                    elif out.get("name") == event.get("away_team"):
                                        away_win_odd = float(out.get("price"))

                            elif m.get("key") == "totals":
                                outcomes = m.get("outcomes", [])
                                for out in outcomes:
                                    if out.get("name") == "Over":
                                        live_over_odd = float(out.get("price"))

                        if home_win_odd and away_win_odd and live_over_odd:
                            return {
                                "home_prematch": home_win_odd,
                                "away_prematch": away_win_odd,
                                "live_odd": live_over_odd
                            }
    except Exception as e:
        print(f"[-] Σφάλμα λήψης Live Odds: {e}", flush=True)

    return None


# ==========================================
# 3. ΜΑΘΗΜΑΤΙΚΟ ΜΟΝΤΕΛΟ POISSON & KELLY
# ==========================================
def calculate_poisson_ev(home_xg, away_xg, minute, odds):
    time_remaining = max(90 - minute, 1) / 90.0
    total_remaining_xg = (home_xg + away_xg) * time_remaining

    # Πιθανότητα να σημειωθεί τουλάχιστον 1 ακόμα γκολ στον αγώνα
    prob_scoring_at_least_one = 1 - poisson.pmf(0, total_remaining_xg)
    ev = (prob_scoring_at_least_one * odds) - 1

    b = odds - 1
    p = prob_scoring_at_least_one
    q = 1 - p
    kelly_stake_pct = max(0, (b * p - q) / b) * 0.25 if b > 0 else 0

    return prob_scoring_at_least_one, ev, kelly_stake_pct


# ==========================================
# 4. ΕΛΕΓΧΟΣ LIVE ΑΓΩΝΩΝ
# ==========================================
def fetch_live_matches():
    headers = {"X-Auth-Token": API_KEY}
    params = {"status": "IN_PLAY"}

    try:
        response = requests.get(API_URL, headers=headers, params=params, timeout=10)
        data = response.json()

        if response.status_code == 200:
            matches = data.get("matches", [])
            print(f"[+] Βρέθηκαν {len(matches)} ζωντανοί αγώνες.", flush=True)
            return matches
        else:
            print(f"[!] Σφάλμα API ({response.status_code}): {data.get('message')}", flush=True)
            return []
    except Exception as e:
        print(f"[-] Σφάλμα API: {e}", flush=True)
        return []


def analyze_matches(live_matches):
    global sent_alerts
    print("[*] Έλεγχος για ζωντανούς αγώνες και ευκαιρίες...", flush=True)

    if isinstance(live_matches, list) and len(live_matches) > 0:
        for match in live_matches:
            match_id = match.get("id")
            if match_id in sent_alerts:
                continue

            home_name = match.get("homeTeam", {}).get("name", "Home")
            away_name = match.get("awayTeam", {}).get("name", "Away")
            match_name = f"{home_name} vs {away_name}"

            score_data = match.get("score", {}).get("fullTime", {})
            match_utc_str = match.get("utcDate")
            status = match.get("status")

            if status == "PAUSED":
                minute = 45
            elif match_utc_str and status == "IN_PLAY":
                start_time = datetime.fromisoformat(match_utc_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                elapsed = int((now - start_time).total_seconds() / 60)
                minute = elapsed if elapsed <= 50 else min(elapsed - 15, 90)
            else:
                minute = 50

            home_goals = score_data.get("home") if score_data.get("home") is not None else 0
            away_goals = score_data.get("away") if score_data.get("away") is not None else 0

            home_xg = float(match.get("home_xg", 1.45))
            away_xg = float(match.get("away_xg", 0.90))
            bankroll = 100

            # -------------------------------------------------------------
            # ΣΕΝΑΡΙΟ 1: HOME FAVORITE RECOVERY (10'-75' & Γηπεδούχος πίσω)
            # -------------------------------------------------------------
            if (10 <= minute <= 75) and (home_goals < away_goals):

                odds_data = get_live_odds_from_odds_api(home_name, away_name)

                if odds_data and isinstance(odds_data, dict):
                    home_pre = odds_data.get("home_prematch")
                    away_pre = odds_data.get("away_prematch")
                    live_odd = odds_data.get("live_odd")

                    # ΕΛΕΓΧΟΣ: Η γηπεδούχος πρέπει να είναι ΠΡΑΓΜΑΤΙΚΑ το φαβορί
                    if home_pre and away_pre and live_odd:
                        if (home_pre < away_pre) and (home_pre <= 1.65):
                            odds_ratio = live_odd / home_pre

                            if 1.20 <= odds_ratio <= 2.60:
                                prob, ev, kelly_pct = calculate_poisson_ev(
                                    home_xg=home_xg,
                                    away_xg=away_xg,
                                    minute=minute,
                                    odds=live_odd
                                )

                                if ev > 0.035:
                                    recommended_stake = round(bankroll * kelly_pct, 2)
                                    msg = (
                                        f"🔥 **HOME FAVORITE RECOVERY ALERT** 🔥\n\n"
                                        f"⚽ **Αγώνας:** {match_name}\n"
                                        f"📊 **Σκορ:** {home_goals}-{away_goals} ({minute}')\n"
                                        f"🎯 **Πίσω στο σκορ:** {home_name} (Γηπεδούχος / Φαβορί)\n"
                                        f"⭐ **Pre-match Odd:** `{home_pre:.2f}` (Φιλοξενούμενη: `{away_pre:.2f}`)\n"
                                        f"📈 **Live Odd (Market):** `{live_odd:.2f}` (Μεταβολή: `x{odds_ratio:.2f}`)\n"
                                        f"💡 **Expected Value (EV):** `+{round(ev * 100, 1)}%`\n"
                                        f"💵 **Προτεινόμενο Ποντάρισμα:** `{recommended_stake}€`"
                                    )
                                    send_telegram_alert(msg)
                                    sent_alerts.add(match_id)
                                    continue

            # -------------------------------------------------------------
            # ΣΕΝΑΡΙΟ 2: Dynamic Poisson EV (55'-85' & Score Diff <= 1)
            # -------------------------------------------------------------
            if (55 <= minute <= 85) and abs(home_goals - away_goals) <= 1:

                odds_data = get_live_odds_from_odds_api(home_name, away_name)
                if odds_data and isinstance(odds_data, dict):
                    live_odds = odds_data.get("live_odd")

                    if live_odds:
                        prob, ev, kelly_pct = calculate_poisson_ev(
                            home_xg=home_xg,
                            away_xg=away_xg,
                            minute=minute,
                            odds=live_odds
                        )

                        total_xg = home_xg + away_xg

                        if ev > 0.035 and total_xg >= 1.20:
                            recommended_stake = round(bankroll * kelly_pct, 2)
                            msg = (
                                f"🚨 **VALUE BET ALERT (LATE GOAL)** 🚨\n\n"
                                f"⚽ **Αγώνας:** {match_name}\n"
                                f"📊 **Σκορ:** {home_goals}-{away_goals} ({minute}')\n"
                                f"📈 **Live Odd:** `{live_odds:.2f}`\n"
                                f"💡 **Expected Value (EV):** `+{round(ev * 100, 1)}%`\n"
                                f"💵 **Προτεινόμενο Ποντάρισμα:** `{recommended_stake}€`"
                            )
                            send_telegram_alert(msg)
                            sent_alerts.add(match_id)


def run_bot():
    print("[*] Το NextGoalBot ξεκίνησε και παρακολουθεί τους αγώνες...", flush=True)

    while True:
        live_matches = fetch_live_matches()

        if live_matches:
            try:
                analyze_matches(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Next Goal Bot: {e}", flush=True)

            try:
                analyze_over_under(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Over/Under Bot: {e}", flush=True)

        time.sleep(180)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()