# football_ou_bot.py
import requests

# Χρησιμοποιούμε τις ίδιες ρυθμίσεις Telegram
TELEGRAM_TOKEN = "6737186847:AAFNhoe3_dhZdig9IjjC3ttGa_yB44Eqdrg"
TELEGRAM_CHAT_ID = "8819668615"
ou_sent_alerts = set()

def send_ou_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[-] Σφάλμα αποστολής Over/Under Alert: {e}")

def analyze_over_under(live_matches):
    global ou_sent_alerts
    print("[*] [Over/Under Bot] Έλεγχος αγώνων...", flush=True)

    if not isinstance(live_matches, list):
        return

    for match in live_matches:
        fixture = match.get("fixture", {})
        teams = match.get("teams", {})
        goals = match.get("goals", {})

        match_id = fixture.get("id")
        if match_id in ou_sent_alerts:
            continue

        home_team = teams.get("home", {}).get("name", "Home")
        away_team = teams.get("away", {}).get("name", "Away")
        match_name = f"{home_team} vs {away_team}"

        home_goals = goals.get("home") if goals.get("home") is not None else 0
        away_goals = goals.get("away") if goals.get("away") is not None else 0
        total_goals = home_goals + away_goals
        minute = fixture.get("status", {}).get("elapsed", 0)

# --- ΣΤΡΑΤΗΓΙΚΗ OVER / UNDER (Παράδειγμα: Over 2.5 στο 60'-75' αν το σκορ είναι 1-1 ή 2-0) ---
        if 60 <= minute <= 75 and total_goals == 2:
            message = (
                f"🚨 *OVER / UNDER ALERT (Over 2.5)*\n"
                f"⚽ **Αγώνας:** {match_name}\n"
                f"⏱ **Λεπτό:** {minute}' | **Σκορ:** {home_goals}-{away_goals}\n"
                f"💡 **Πρόταση:** Over 2.5 Goals\n"
            )
            send_ou_alert(message)
            ou_sent_alerts.add(match_id)