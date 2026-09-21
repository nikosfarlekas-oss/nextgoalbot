# NextGoalBot ⚽🤖

An automated, real-time football live-betting intelligence service built in Python. The system monitors live match telemetry, calculates match tempo dynamics, evaluates odds against mathematical value models, and dispatches real-time alerts via Telegram.

---

## 🚀 Key Features

* **Real-Time Match Telemetry:** Ingests live match scores, elapsed time, and status using the `football-data.org` API.
* **Live Tempo & Intensity Modeling:** Integrates `Highlightly API` to monitor shots on target and dynamically weight Expected Goals ($xG$) based on live momentum.
* **Quantitative Value & Risk Management:**
  * **Poisson Distribution Engine:** Calculates remaining goal probabilities.
  * **Expected Value (+EV) Filtering:** Evaluates bookmaker odds in real-time to trigger alerts only when positive EV thresholds are met.
  * **Quarter-Kelly Staking:** Automated bankroll management using $25\%$ Fractional Kelly Criterion capped at $3\%$ maximum stake.
* **Automated Settlement & Logging:** Thread-safe asynchronous tracking system that automatically settles completed matches and logs detailed PnL metrics to CSV.
* **Cloud & Web Architecture:** Embedded Flask Web Server hosting lightweight `/health` endpoints for continuous uptime monitoring on platforms like Render.

---

## 🛠 Tech Stack & APIs

* **Core Language:** Python 3.10+
* **Libraries:** `Flask`, `requests`, `scipy`, `threading`
* **Integrations:**
  * [football-data.org API](https://www.football-data.org/) — Live match state & settlement
  * [The Odds API](https://the-odds-api.com/) — Live & Pre-match odds integration
  * [Highlightly API](https://highlightly.net/) — Live match statistics & shots telemetry
  * [Telegram Bot API](https://core.telegram.org/bots/api) — Automated notification channel

---

## 📁 Repository Structure

```text
.
├── main.py              # Main loop, Flask server, Poisson EV engine & settlement tracker
├── football_ou_bot.py   # Over/Under target strategy & Telegram notification handlers
├── requirements.txt     # Dependency manifest
└── README.md            # System documentation
