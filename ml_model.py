# =========================================================
# AUTOMATED SELF-LEARNING MACHINE LEARNING MODEL (ml_model.py)
# RANDOM FOREST CLASSIFIER - BUG-FIXED EDITION
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
    """ Безопасно зареждане на тренираните изкуствени интелекти """
    global btts_model, over_model

    if os.path.exists(BTTS_MODEL):
        btts_model = joblib.load(BTTS_MODEL)
        print("📊 BTTS Model Loaded Successfully.")
    else:
        print("⚠️ BTTS Model (.pkl) not found. Running on fallback baseline.")

    if os.path.exists(OVER_MODEL):
        over_model = joblib.load(OVER_MODEL)
        print("📊 OVER 2.5 Model Loaded Successfully.")
    else:
        print("⚠️ OVER Model (.pkl) not found. Running on fallback baseline.")

    print("🔥 AI v1000 READY")


def make_features(shots_h, shots_a, att_h, att_a, goals):
    """ 
    Уеднаквена математическа матрица за превръщане на статистиката в числови вектори.
    Премахва риска от грешно мапване на речници (Dictionaries).
    """
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
    """ Автоматично претрениране на модела с новите данни от изминалия ден """
    if not os.path.exists(DATA_FILE):
        print("❌ Критична грешка: dataset.json липсва. Няма данни за обучение!")
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Грешка при четене на dataset.json: {e}")
        return

    if len(data) < 100:
        print(f"⚠️ Базата данни съдържа само {len(data)} записа. Трябват минимум 100+ за обучение!")
        return

    X, y_btts, y_over = [], [], []

    for d in data:
        # Извличане по твърди ключове от твоя оригинален JSON формат
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

    # Обучение на модела за Гол/Гол (BTTS)
    btts = RandomForestClassifier(n_estimators=120, random_state=42)
    btts.fit(X, y_btts)
    joblib.dump(btts, BTTS_MODEL)

    # Обучение на модела за Над 2.5 гола
    over = RandomForestClassifier(n_estimators=120, random_state=42)
    over.fit(X, y_over)
    joblib.dump(over, OVER_MODEL)

    print(f"🔥 MODELS TRAINED SUCCESSFULLY ON {len(data)} SAMPLES!")


def predict_btts(sh, sa, ah, aa, goals):
    """ Изчислява вероятност за Гол/Гол на живо през Random Forest """
    if btts_model is None:
        return None
    features = make_features(sh, sa, ah, aa, goals)
    try:
        # Връщаме вероятността за клас '1' (мачът да завърши Гол/Гол)
        return btts_model.predict_proba([features])[0][1]
    except Exception as e:
        print("Error during predict_btts:", e)
        return None


def predict_over(sh, sa, ah, aa, goals):
    """ Изчислява вероятност за Над 2.5 гола на живо през Random Forest """
    if over_model is None:
        return None
    features = make_features(sh, sa, ah, aa, goals)
    try:
        # Връщаме вероятността за клас '1' (мачът да завърши Над 2.5)
        return over_model.predict_proba([features])[0][1]
    except Exception as e:
        print("Error during predict_over:", e)
        return None
