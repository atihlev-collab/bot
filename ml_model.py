Main5 ML layer: BTTS and Over 2.5 specialist models."""
import json, os
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
        try: btts_model = joblib.load(BTTS_MODEL)
        except Exception: btts_model = None
    if os.path.exists(OVER_MODEL):
        try: over_model = joblib.load(OVER_MODEL)
        except Exception: over_model = None
    print("рџ”Ґ Main5 ML READY")

def make_features(d):
    def n(k):
        try: return float(d.get(k, 0) or 0)
        except Exception: return 0.0
    sh, sa, ah, aa, goals = n("shots_h"), n("shots_a"), n("att_h"), n("att_a"), n("goals")
    return [sh, sa, ah, aa, goals, sh+sa, ah+aa, abs(sh-sa)]

def train_model():
    if not os.path.exists(DATA_FILE):
        return False
    try: data = json.load(open(DATA_FILE, encoding="utf-8"))
    except Exception: return False
    if len(data) < 100: return False
    X, yb, yo = [], [], []
    for d in data:
        try:
            X.append(make_features(d)); yb.append(int(d["btts"])); yo.append(int(d["over25"]))
        except Exception: continue
    if len(X) < 100: return False
    X = np.asarray(X)
    btts = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
    over = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
    btts.fit(X, yb); over.fit(X, yo)
    joblib.dump(btts, BTTS_MODEL); joblib.dump(over, OVER_MODEL)
    return True

def _predict(model, features):
    if model is None: return None
    try:
        classes = list(model.classes_)
        if 1 not in classes: return 0.0
        return float(model.predict_proba([features])[0][classes.index(1)])
    except Exception:
        return None

def predict_btts(sh, sa, ah, aa, goals):
    return _predict(btts_model, make_features({"shots_h":sh,"shots_a":sa,"att_h":ah,"att_a":aa,"goals":goals}))

def predict_over(sh, sa, ah, aa, goals):
    return _predict(over_model, make_features({"shots_h":sh,"shots_a":sa,"att_h":aa,"att_a":aa,"goals":goals}))
