"""Main5 AI decision layer.
ML is an auxiliary signal, not a hard dependency for the whole bot.
"""
from ml_model import predict_btts, predict_over

def ai_decision(sh, sa, ah, aa, goals, odds, league=None):
    btts = predict_btts(sh, sa, ah, aa, goals)
    over = predict_over(sh, sa, ah, aa, goals)

    total_att = max(0.0, ah + aa)
    total_sh = max(0.0, sh + sa)
    tempo = min(1.0, total_att / 100.0)
    activity = min(1.0, total_sh / 20.0)

    candidates = []

    if btts is not None:
        score = btts * 0.65 + tempo * 0.20 + activity * 0.15
        if score >= 0.60 and 1.50 <= odds <= 4.00:
            candidates.append(("BTTS", round(score, 4)))

    if over is not None:
        score = over * 0.65 + tempo * 0.25 + activity * 0.10
        if score >= 0.58 and 1.40 <= odds <= 4.00:
            candidates.append(("Over 2.5", round(score, 4)))

    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])
