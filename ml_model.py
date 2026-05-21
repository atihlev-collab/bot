import os
import json
import joblib
from sklearn.ensemble import RandomForestClassifier

def make_features(sh, sa, ah, aa, total_goals):
    return [[sh, sa, ah, aa, total_goals]]

def predict_btts(sh, sa, ah, aa, total_goals):
    try:
        model = load_model("ml_btts.pkl")
        if model:
            features = make_features(sh, sa, ah, aa, total_goals)
            return float(model.predict_proba(features)[0][1])
    except: pass
    return 0.54

def predict_over(sh, sa, ah, aa, total_goals):
    try:
        model = load_model("ml_over.pkl")
        if model:
            features = make_features(sh, sa, ah, aa, total_goals)
            return float(model.predict_proba(features)[0][1])
    except: pass
    return 0.51

def load_model(filename):
    if os.path.exists(filename):
        try: return joblib.load(filename)
        except: return None
    return None

def train_model():
    dataset_file = "dataset.json"
    if not os.path.exists(dataset_file): return
    try:
        with open(dataset_file, "r") as f: data = json.load(f)
        if len(data) < 10: return
        X, y_btts, y_over = [], [], []
        for item in data:
            X.append([item["sh"], item["sa"], item["ah"], item["aa"], item.get("trigger_total_goals", 0)])
            y_btts.append(1 if item.get("final_home_goals", 0) > 0 and item.get("final_away_goals", 0) > 0 else 0)
            y_over.append(1 if (item.get("final_home_goals", 0) + item.get("final_away_goals", 0)) > 2.5 else 0)
        
        clf_btts = RandomForestClassifier(n_estimators=100, random_state=42)
        clf_btts.fit(X, y_btts)
        joblib.dump(clf_btts, "ml_btts.pkl")
        
        clf_over = RandomForestClassifier(n_estimators=100, random_state=42)
        clf_over.fit(X, y_over)
        joblib.dump(clf_over, "ml_over.pkl")
        print("🧠 [ML ENGINE] Моделите бяха обучени успешно!")
    except: pass
