Nikos
Farlekas < nikosfarlekas @ gmail.com >

9: 01 μ.μ.(πριν
από
0
λεπτά)


προς
εγώ
import requests
import time
from scipy.stats import poisson

# ==========================================
# 1. TELEGRAM SETUP & CONFIG
# ==========================================
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # Βάλε το Token σου
CHAT_ID = "YOUR_CHAT_ID"  # Βάλε το Chat ID σου

API_KEY = "d0e4efba8fmsh3352e4aaa285454p156c58jsnf98469d47643"
API_HOST = "free-livescore-api.p.rapidapi.com"

bankroll = 100.0  # Υποθετικό κεφάλαιο σε Ευρώ
sent_alerts = set()


def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
try:
    requests.post(url, json=payload)
except Exception as e:
    print(f"[-] Σφάλμα αποστολής στο Telegram: {e}")


# ==========================================
# 2. ΜΟΝΤΕΛΟ POISSON & KELLY CRITERION
# ==========================================
def calculate_poisson_ev(home_xg, away_xg, minute, current_home_goals, odds):
    remaining_time = max(1, 90 - minute)


time_factor = remaining_time / 90.0

# Προσαρμογή προσδοκώμενων γκολ βάσει εναπομείναντος χρόνου
exp_goals_home = home_xg * time_factor

# Πιθανότητα για τουλάχιστον +1 γκολ γηπεδούχου (Over 0.5 additional goal)
prob = 1 - poisson.pmf(0, exp_goals_home)

# Υπολογισμός Expected Value (EV)
ev = (prob * odds) - 1

# Kelly Criterion (Fractional 25% για διαχείριση ρίσκου)
b = odds - 1
kelly_pct = ((b * prob - (1 - prob)) / b) * 0.25 if b > 0 else 0
kelly_pct = max(0, kelly_pct)

return prob, ev, kelly_pct


# ==========================================
# 3. LIVE API FETCH & ΑΝΑΛΥΣΗ ΑΓΩΝΩΝ
# ==========================================
def fetch_live_matches():
    url = "https://free-livescore-api.p.rapidapi.com/livescore-get-search"


querystring = {"sportname": "soccer", "search": "live"}
headers = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": API_HOST
}
try:
    response = requests.get(url, headers=headers, params=querystring)
if response.status_code == 200:
    return response.json()
return []
except Exception as e:
print(f"[-] Σφάλμα API: {e}")
return []


def analyze_matches():
    print("[*] Έλεγχος για ζωντανούς αγώνες και ευκαιρίες EV...")


data = fetch_live_matches()

if isinstance(data, list) and len(data) > 0:
    for match in data:
        match_id = match.get("id", match.get("title"))

if match_id in sent_alerts:
    continue

# Άντληση ή υπολογισμός βασικών στοιχείων (defaults για τα πεδία του API)
minute = int(match.get("minute", 60))
home_xg = float(match.get("home_xg", 1.65))
away_xg = float(match.get("away_xg", 0.85))
odds = float(match.get("home_odds_over_05", 1.85))

# Εκτέλεση του Poisson & Kelly υπολογισμού
prob, ev, kelly_pct = calculate_poisson_ev(
    home_xg=home_xg,
    away_xg=away_xg,
    minute=minute,
    current_home_goals=0,
    odds=odds
)

# Φίλτρο Αξίας: Αποστολή ειδοποίησης ΜΟΝΟ αν υπάρχει Value Bet (EV > 8%)
if ev > 0.08 and home_xg >= 1.50:
    recommended_stake = round(bankroll * kelly_pct, 2)
match_name = match.get("title", "Unknown Match")
score = match.get("score", "0-0")

msg = (
    f"🚨 **VALUE BET ALERT** 🚨\n\n"
    f"⚽ **Αγώνας:** {match_name}\n"
    f"📊 **Σκορ:** {score} ({minute}')\n"
    f"📈 **xG Γηπεδούχου:** {home_xg}\n"
    f"🎯 **Πιθανότητα Next Goal:** {round(prob * 100, 1)}%\n"
    f"💰 **Απόδοση:** {odds}\n"
    f"💡 **Expected Value (EV):** +{round(ev * 100, 1)}%\n"
    f"💵 **Προτεινόμενο Ποντάρισμα (Kelly):** {recommended_stake}€"
)

send_telegram_alert(msg)
sent_alerts.add(match_id)
else:
print("[-] Δεν βρέθηκαν ζωντανοί αγώνες αυτή τη στιγμή.")


# ==========================================
# 4. MAIN LOOP
# ==========================================
def run_bot():
    print("[*] Το NextGoalBot ξεκίνησε και παρακολουθεί τους αγώνες...")


while True:
    try:
        analyze_matches()
except Exception as e:
print(f"[-] Σφάλμα κατά τον έλεγχο: {e}")

time.sleep(60)

if __name__ == "__main__":
    run_bot()

