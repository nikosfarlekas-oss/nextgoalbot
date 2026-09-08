import math
import requests
from scipy.stats import poisson
import time
import os
import threading
from flask import Flask
from football_ou_bot import analyze_over_under

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)



# ==========================================
# 1. ΡΥΘΜΙΣΕΙΣ TELEGRAM BOT
# ==========================================

TELEGRAM_TOKEN = "8737186847:AAFNhoe3_dhZdig9IjjC3ttGa_yB44Eqdrg"
TELEGRAM_CHAT_ID = "8819668615"
sent_alerts = set() # Λίστα για να μην στέλνει διπλά alerts

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN:
        print("[!] Προειδοποίηση: Δεν έχεις βάλει το Telegram Token!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID,
               "text": message,
               "parse_mode": "Markdown"
               }
    try:
        requests.post(url, json=payload, timeout=5)
        print("[+] Το alert στάλθηκε επιτυχώς στο Telegram!")
    except Exception as e:
        print(f"[-] Σφάλμα κατά τη αποστολή στο Telegram: {e}")


# ==========================================
# 2. ΜΑΘΗΜΑΤΙΚΟ ΜΟΝΤΕΛΟ POISSON & KELLY
# ==========================================
def calculate_poisson_ev(home_xg, away_xg, minute, current_home_goals, odds):
    # Υπολογισμός εναπομείναντος χρόνου
    time_remaining = max(90 - minute, 1) / 90.0
    # Προσαρμοσμένο xG για τα λεπτά που απομένουν
    remaining_home_xg = home_xg * time_remaining
    # Πιθανότητα η γηπεδούχος να βάλει τουλάχιστον 1 ακόμα γκολ (Poisson)
    prob_scoring_at_least_one = 1 - poisson.pmf(0, remaining_home_xg)
    # Υπολογισμός Expected Value (EV)
    ev = (prob_scoring_at_least_one * odds) - 1
    # Υπολογισμός Fractional Kelly Criterion (1/4 Kelly για ασφάλεια)
    b = odds - 1
    p = prob_scoring_at_least_one
    q = 1 - p
    kelly_stake_pct = max(0, (b * p - q) / b) * 0.25

    return prob_scoring_at_least_one, ev, kelly_stake_pct


# ==========================================
# 3. ΕΛΕΓΧΟΣ LIVE ΑΓΩΝΩΝ (TEST RUN)
# ==========================================
API_KEY = "1d603b2042b54e678deee240f4819860"
API_URL = "https://v3.football.api-sports.io/fixtures?live=all"

def fetch_live_matches():
    headers = {
        "x-apisports-key": API_KEY,
    }
    try:
        response = requests.get(API_URL, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()

            print(f"[DEBUG] API Errors: {data.get('errors')}", flush=True)
            print(f"[DEBUG] API Results: {data.get('results')}", flush=True)

            return data.get("response", [])
        else:
            print(f"[-] API Error Status: {response.status_code}")
            return []

    except Exception as e:
        print(f"[-] Σφάλμα API: {e}")
        return []



def analyze_matches(live_matches):
        global sent_alerts
        print("[*] Έλεγχος για ζωντανούς αγώνες και ευκαιρίες...")
        data = fetch_live_matches()

        print(f"[*] API Status: Found {len(data) if isinstance(data, list) else 0} live matches", flush=True)

        if isinstance(data, list) and len(data) > 0:
            for match in data:
                match_id = match.get("id", match.get("title"))

                if match_id in sent_alerts:
                    continue

                match_name = match.get("title", "Unknown Match")
                score = match.get("score", "0-0")
                minute = int(match.get("minute", 30))

                # Διαχωρισμός σκορ
                home_goals = 0
                away_goals = 0
                if "-" in score:
                    try:
                        parts = score.split("-")
                        home_goals = int(parts[0].strip())
                        away_goals = int(parts[1].strip())
                    except:
                        pass

                # Pre-match & Live Αποδόσεις
                home_prematch_odds = float(match.get("home_prematch_odds", 1.50))
                live_next_goal_odds = float(match.get("home_odds_over_05", 1.85))  # Απόδοση live για επόμενο γκολ
                home_xg = float(match.get("home_xg", 1.50))
                away_xg = float(match.get("away_xg", 0.80))

                # Dynamic Ελάχιστη Αποδεκτή Απόδοση βάσει λεπτού
                min_required_odds = 1.70 if minute <= 45 else 1.95

                # -------------------------------------------------------------
                # ΚΑΝΟΝΑΣ 1: Early Favorite Conceded (Μέσα στα όρια της απόδοσης)
                # -------------------------------------------------------------
                if (home_goals < away_goals and
                    home_prematch_odds <= 1.70 and
                    minute <= 70 and
                    live_next_goal_odds >= min_required_odds):

                    msg = (
                        f"🔥 **HOT RECOVERY BET ALERT** 🔥\n\n"
                        f"⚽ **Αγώνας:** {match_name}\n"
                        f"📊 **Σκορ:** {score} ({minute}')\n"
                        f"⭐ **Pre-match Απόδοση:** {home_prematch_odds}\n"
                        f"💰 **Live Απόδοση (Next Goal):** {live_next_goal_odds} (Όριο: {min_required_odds})\n"
                        f"⚠️ **Το φαβορί δέχθηκε γκολ & η απόδοση έχει VALUE!**"
                    )
                    send_telegram_alert(msg)
                    sent_alerts.add(match_id)
                    continue

                # -------------------------------------------------------------
                # ΚΑΝΟΝΑΣ 2: Κλασικός Υπολογισμός Value Bet (Poisson & Kelly)
                # -------------------------------------------------------------
                prob, ev, kelly_pct = calculate_poisson_ev(
                     home_xg=home_xg,
                     away_xg=away_xg,
                     minute=minute,
                     current_home_goals=home_goals,
                     odds=live_next_goal_odds
                 )
                bankroll = 100
                total_xg = home_xg + away_xg
                if ev > 0.035 and total_xg >= 1.20 and (50 <= minute <= 82):
                    recommended_stake = round(bankroll * kelly_pct, 2)
                    msg = (
                        f"🚨 **VALUE BET ALERT** 🚨\n\n"
                        f"⚽ **Αγώνας:** {match_name}\n"
                        f"📊 **Σκορ:** {score} ({minute}')\n"
                        f"💡 **Expected Value (EV):** +{round(ev * 100, 1)}%\n"
                         f"💵 **Προτεινόμενο Ποντάρισμα:** {recommended_stake}€"
                     )
                    send_telegram_alert(msg)
                    sent_alerts.add(match_id)



def run_bot():
    print("[*] Το NextGoalBot ξεκίνησε και παρακολουθεί τους αγώνες...", flush=True)

    while True:
        print("[*] Fetching live matches...", flush=True)
        live_matches = fetch_live_matches()
        print(f"[+] Βρέθηκαν {len(live_matches)} ζωντανοί αγώνες.", flush=True)

        if live_matches:
            # 1. Εκτέλεση του Next Goal Bot
            try:
                analyze_matches(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Next Goal Bot: {e}", flush=True)

            # 2. Εκτέλεση του Over/Under Bot
            try:
                analyze_over_under(live_matches)
            except Exception as e:
                print(f"[-] Σφάλμα στο Over/Under Bot: {e}", flush=True)

        # Περιμένει 270 δευτερόλεπτα πριν τον επόμενο έλεγχο
        time.sleep(300)

if __name__ == "__main__":
    # Ξεκινάει ο Flask server παράλληλα
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()