from ml_model import predict_btts, predict_over

def ai_decision(sh, sa, ah, aa, goals, odds, league):

    btts = predict_btts(sh, sa, ah, aa, goals)
    over = predict_over(sh, sa, ah, aa, goals)

    if btts is None or over is None:
        return None

    total_att = ah + aa
    total_sh = sh + sa

    tempo = total_att / 50
    activity = total_sh / 10

    score_btts = btts * 0.5 + tempo * 0.3 + activity * 0.2
    score_over = over * 0.6 + tempo * 0.3 + activity * 0.1

    if score_btts > 0.60 and 1.6 <= odds <= 2.3:
        return "BTTS", score_btts

    if score_over > 0.58 and 1.5 <= odds <= 2.5:
        return "Over 2.5", score_over

    return None