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
        print("[!] Προειδοποίηση: Δεν έχεις βάλει το Telegram Token!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
        print("[+] Το alert στάλθηκε επιτυχώς στο Telegram!")
    except Exception as e:
        print(f"[-] Σφάλμα κατά τη αποστολή στο Telegram: {e}")


# ==========================================
# 2. ΗΒΡΙΔΙΚΗ ΛΗΨΗ LIVE ODDS (THE ODDS API)
# ==========================================
def get_live_odds_from_odds_api(home_team, away_team):
    """Τραβάει ζωντανές αποδόσεις Over μόνο όταν χρειάζεται."""
    if not ODDS_API_KEY:
        print("[INFO] Δεν βρέθηκε ODDS_API_KEY, χρήση default odds (1.85)")
        return 1.85

    try:
        url = f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=totals&live=true"
        res = requests.get(url, timeout=5)

        remaining_requests = res.headers.get("x-requests-remaining", "N/A")
        print(f"[ODDS API] Επιτυχές request. Απομένουν: {remaining_requests} requests", flush=True)

        if res.status_code == 200:
            events = res.json()
            for event in events:
                h_name = event.get("home_team", "").lower()
                a_name = event.get("away_team", "").lower()

                # Loose matching στα ονόματα
                if home_team.lower() in h_name or away_team.lower() in a_name:
                    bookmakers = event.get("bookmakers", [])
                    if bookmakers:
                        markets = bookmakers[0].get("markets", [])
                        for m in markets:
                            if m.get("key") == "totals":
                                outcomes = m.get("outcomes", [])
                                for out in outcomes:
                                    if out.get("name") == "Over":
                                        price = float(out.get("price", 1.85))
                                        print(f"[ODDS API] Live Odd: {price} για {home_team} vs {away_team}",
                                              flush=True)
                                        return price
    except Exception as e:
        print(f"[-] Σφάλμα λήψης Live Odds: {e}", flush=True)

    return 1.85


# ==========================================
# 3. ΜΑΘΗΜΑΤΙΚΟ ΜΟΝΤΕΛΟ POISSON & KELLY
# ==========================================
def calculate_poisson_ev(home_xg, away_xg, minute, current_home_goals, odds):
    time_remaining = max(90 - minute, 1) / 90.0
    remaining_home_xg = home_xg * time_remaining

    # Πιθανότητα για τουλάχιστον 1 ακόμα γκολ
    prob_scoring_at_least_one = 1 - poisson.pmf(0, remaining_home_xg)

    # Expected Value
    ev = (prob_scoring_at_least_one * odds) - 1

    # Fractional Kelly (1/4 Kelly)
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
            print(f"[+] Βρέθηκαν {len(matches)} ζωντανοί αγώνες.")
            return matches
        else:
            print(f"[!] Σφάλμα API ({response.status_code}): {data.get('message')}")
            return []
    except Exception as e:
        print(f"[-] Σφάλμα API: {e}")
        return []


def analyze_matches(live_matches):
    global sent_alerts
    print("[*] Έλεγχος για ζωντανούς αγώνες και ευκαιρίες...")

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

                if elapsed <= 50:
                    minute = elapsed
                else:
                    minute = min(elapsed - 15, 90)
            else:
                minute = 50

            home_goals = score_data.get("home") if score_data.get("home") is not None else 0
            away_goals = score_data.get("away") if score_data.get("away") is not None else 0

            home_prematch_odds = float(match.get("home_prematch_odds", 1.50))
            home_xg = float(match.get("home_xg", 1.50))
            away_xg = float(match.get("away_xg", 0.80))

            min_required_odds = 1.70 if minute <= 45 else 1.95

            # -------------------------------------------------------------
            # ΚΑΝΟΝΑΣ 1: Early Favorite Conceded
            # -------------------------------------------------------------
            if (home_goals < away_goals and
                    home_prematch_odds <= 1.70 and
                    minute <= 70):

                # Τραβάμε live odd μόνο αν πληροί τα βασικά κριτήρια
                live_odds = get_live_odds_from_odds_api(home_name, away_name)

                if live_odds >= min_required_odds:
                    msg = (
                        f"🔥 **HOT RECOVERY BET ALERT** 🔥\n\n"
                        f"⚽ **Αγώνας:** {match_name}\n"
                        f"📊 **Σκορ:** {home_goals}-{away_goals} ({minute}')\n"
                        f"⭐ **Pre-match Απόδοση:** {home_prematch_odds}\n"
                        f"💰 **Live Απόδοση (Next Goal):** {live_odds:.2f} (Όριο: {min_required_odds})\n"
                        f"⚠️ **Το φαβορί δέχθηκε γκολ & η απόδοση έχει VALUE!**"
                    )
                    send_telegram_alert(msg)
                    sent_alerts.add(match_id)
                    continue

            # -------------------------------------------------------------
            # ΚΑΝΟΝΑΣ 2: Dynamic Poisson EV (55'-85' & Score Diff <= 1)
            # -------------------------------------------------------------
            if (55 <= minute <= 85) and abs(home_goals - away_goals) <= 1:

                # Τραβάμε ΠΡΑΓΜΑΤΙΚΗ live απόδοση από το Odds API
                live_odds = get_live_odds_from_odds_api(home_name, away_name)

                prob, ev, kelly_pct = calculate_poisson_ev(
                    home_xg=home_xg,
                    away_xg=away_xg,
                    minute=minute,
                    current_home_goals=home_goals,
                    odds=live_odds
                )

                bankroll = 100
                total_xg = home_xg + away_xg

                if ev > 0.035 and total_xg >= 1.20:
                    recommended_stake = round(bankroll * kelly_pct, 2)
                    msg = (
                        f"🚨 **VALUE BET ALERT (LATE GOAL)** 🚨\n\n"
                        f"⚽ **Αγώνας:** {match_name}\n"
                        f"📊 **Σκορ:** {home_goals}-{away_goals} ({minute}')\n"
                        f"📈 **Live Odd:** {live_odds:.2f}\n"
                        f"💡 **Expected Value (EV):** +{round(ev * 100, 1)}%\n"
                        f"💵 **Προτεινόμενο Ποντάρισμα:** {recommended_stake}€"
                    )
                    send_telegram_alert(msg)
                    sent_alerts.add(match_id)


def run_bot():
    print("[*] Το NextGoalBot ξεκίνησε και παρακολουθεί τους αγώνες...", flush=True)

    while True:
        live_matches = fetch_live_matches()

        if live_matches:
            # 1. Εκτέλεση Next Goal Bot
            try:
                analyze_matches(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Next Goal Bot: {e}", flush=True)

            # 2. Εκτέλεση Over/Under Bot
            try:
                analyze_over_under(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Over/Under Bot: {e}", flush=True)

        # Περιμένει 180 δευτερόλεπτα (3 λεπτά) πριν τον επόμενο κύκλο
        time.sleep(180)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()