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

    if os.path.exists(OVER_MODEL):
        over_model = joblib.load(OVER_MODEL)

    print("🔥 AI v1000 READY")


def make_features(d):
    return [
        d["shots_h"],
        d["shots_a"],
        d["att_h"],
        d["att_a"],
        d["goals"],
        d["shots_h"] + d["shots_a"],
        d["att_h"] + d["att_a"],
        abs(d["shots_h"] - d["shots_a"])
    ]


def train_model():

    if not os.path.exists(DATA_FILE):
        print("❌ no dataset")
        return

    data = json.load(open(DATA_FILE))

    if len(data) < 100:
        print("⚠️ need 100+ samples")
        return

    X, y_btts, y_over = [], [], []

    for d in data:
        X.append(make_features(d))
        y_btts.append(d["btts"])
        y_over.append(d["over25"])

    X = np.array(X)

    btts = RandomForestClassifier(n_estimators=120)
    btts.fit(X, y_btts)
    joblib.dump(btts, BTTS_MODEL)

    over = RandomForestClassifier(n_estimators=120)
    over.fit(X, y_over)
    joblib.dump(over, OVER_MODEL)

    print("🔥 MODELS TRAINED")


def predict_btts(sh, sa, ah, aa, goals):
    if btts_model is None:
        return None
    d = {"shots_h": sh, "shots_a": sa, "att_h": ah, "att_a": aa, "goals": goals}
    return btts_model.predict_proba([make_features(d)])[0][1]


def predict_over(sh, sa, ah, aa, goals):
    if over_model is None:
        return None
    d = {"shots_h": sh, "shots_a": sa, "att_h": ah, "att_a": aa, "goals": goals}
    return over_model.predict_proba([make_features(d)])[0][1]