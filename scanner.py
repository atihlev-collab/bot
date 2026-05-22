import requests
from datetime import datetime

BASE_URL = "https://v3.football.api-sports.io"

def get_matches(headers):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        r = requests.get(f"{BASE_URL}/fixtures?date={today}", headers=headers).json()
        return r.get("response", [])
    except:
        return []

def get_team_form(team_id, headers):
    try:
        r = requests.get(
            f"{BASE_URL}/fixtures?team={team_id}&last=5",
            headers=headers
        ).json()

        games = r.get("response", [])
        goals = 0
        conceded = 0

        for g in games:
            if g["teams"]["home"]["id"] == team_id:
                goals += g["goals"]["home"] or 0
                conceded += g["goals"]["away"] or 0
            else:
                goals += g["goals"]["away"] or 0
                conceded += g["goals"]["home"] or 0

        if len(games) == 0:
            return 0, 0

        return goals / len(games), conceded / len(games)

    except:
        return 0, 0


def analyze_match(match, headers):

    fid = match["fixture"]["id"]

    home_id = match["teams"]["home"]["id"]
    away_id = match["teams"]["away"]["id"]

    # FORM
    h_scored, h_conceded = get_team_form(home_id, headers)
    a_scored, a_conceded = get_team_form(away_id, headers)

    # STATS
    try:
        sr = requests.get(
            f"{BASE_URL}/fixtures/statistics?fixture={fid}",
            headers=headers
        ).json()

        if not sr.get("response"):
            return None

        s = sr["response"]

        sh = int(s[0]["statistics"][2]["value"] or 0)
        sa = int(s[1]["statistics"][2]["value"] or 0)

        ah = int(s[0]["statistics"][0]["value"] or 0)
        aa = int(s[1]["statistics"][0]["value"] or 0)

    except:
        return None

    total_shots = sh + sa
    total_att = ah + aa

    form_goals = h_scored + a_scored
    form_conceded = h_conceded + a_conceded

    balance = abs(sh - sa)

    score_over = (
        total_shots * 2 +
        total_att * 0.3 +
        form_goals * 5 +
        form_conceded * 2
    )

    score_btts = (
        total_shots * 1.5 +
        form_goals * 6 +
        form_conceded * 3 -
        balance * 1.5
    )

    prob_over = min(80, 50 + score_over / 10)
    prob_btts = min(80, 50 + score_btts / 10)

    results = []

    if prob_over >= 74 and total_shots >= 6 and total_att >= 50:
        results.append(("Over 2.5", prob_over, 2.0))

    if prob_btts >= 72 and balance <= 3:
        results.append(("BTTS", prob_btts, 1.9))

    if not results:
        return None

    return results
