# =========================================================
# AUTOMATED SELF-LEARNING MACHINE LEARNING MODEL (ml_model.py)
# RANDOM FOREST CLASSIFIER - PRO ULTRA EDITION
# =========================================================

import json
import os
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

BTTS_MODEL = "ml_btts.pkl"
OVER_MODEL = "ml_over.pkl"
DATA_FILE = "dataset.json"

btts_model = None
over_model = None

def load_model():
    global btts_model, over_model

    if os.path.exists(BTTS_MODEL):
        btts_model = joblib.load(BTTS_MODEL)
        print("✅ AI Моделът за Гол/Гол е зареден успешно.")
    else:
        print("⚠️ Липсва ml_btts.pkl. Използва се математическа базова линия.")

    if os.path.exists(OVER_MODEL):
        over_model = joblib.load(OVER_MODEL)
        print("✅ AI Моделът за Над 2.5 е зареден успешно.")
    else:
        print("⚠️ Липсва ml_over.pkl. Използва се математическа базова линия.")

    print("🔥 AI PRO v1000 READY")


def make_features(shots_h, shots_a, att_h, att_a, goals):
    """ Уеднаквена математическа матрица за премахване на риска от разминаване """
    total_shots = shots_h + shots_a
    total_attacks = att_h + att_a
    shots_diff = abs(shots_h - shots_a)
    
    return [
        shots_h,
        shots_a,
        att_h,
        att_a,
        goals,
        total_shots,
        total_attacks,
        shots_diff
    ]


def train_model():
    if not os.path.exists(DATA_FILE):
        print("❌ no dataset.json found yet.")
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Грешка при четене на базата: {e}")
        return

    if len(data) < 100:
        print(f"⚠️ Малко данни ({len(data)} записа). Моделът ще се тренира след 100+ мача.")
        return

    X, y_btts, y_over = [], [], []

    for d in data:
        features = make_features(
            d.get("shots_h", 0), 
            d.get("shots_a", 0), 
            d.get("att_h", 0), 
            d.get("att_a", 0), 
            d.get("goals", 0)
        )
        X.append(features)
        y_btts.append(d.get("btts", 0))
        y_over.append(d.get("over25", 0))

    X = np.array(X)

    btts = RandomForestClassifier(n_estimators=120, random_state=42)
    btts.fit(X, y_btts)
    joblib.dump(btts, BTTS_MODEL)

    over = RandomForestClassifier(n_estimators=120, random_state=42)
    over.fit(X, y_over)
    joblib.dump(over, OVER_MODEL)

    print(f"🔥 ИИ УСПЕШНО ПРЕТРЕНИРАН ВЪРХУ {len(data)} МАЧА!")


def predict_btts(sh, sa, ah, aa, goals):
    if btts_model is None: return None
    features = make_features(sh, sa, ah, aa, goals)
    try:
        return btts_model.predict_proba([features])[0][1]
    except: return None


def predict_over(sh, sa, ah, aa, goals):
    if over_model is None: return None
    features = make_features(sh, sa, ah, aa, goals)
    try:
        return over_model.predict_proba([features])[0][1]
    except: return None

