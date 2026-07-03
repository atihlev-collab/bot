#=========================================================
# MAIN V3
# CLEAN BETTING SYSTEM
# =========================================================

import requests
import sqlite3
import asyncio
import threading
import time
import logging

 

from scipy.stats import poisson
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

bot = Bot(token=BOT_TOKEN)

logging.basicConfig(level=logging.WARNING)

# =========================================================
# LEAGUE FILTERS
# =========================================================

BLOCKED_WORDS = [

    "women",
    "female",

    "youth",
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u23",
    "friendly",
    "friendlies",
    "u22",
    "u24",
    "olympic", 
    "reserve",
    "reserves",
    "academy",
    "amateur"
]

BAD_COUNTRIES = [

    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia",

    "Russia",
    "Belarus",
    "Israel",
    "Nicaragua",
    "Guatemala",
    "Honduras",
    "El Salvador"
]


# =========================================================
# CACHE
# =========================================================

sent_live = {}

sent_prematch = {}
team_form_cache = {}

# =========================================================
# DATABASE
# =========================================================

def init_database():

    conn = sqlite3.connect(
        "v3_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS signals (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        fixture_id INTEGER,

        country TEXT,
        league TEXT,

        home_team TEXT,
        away_team TEXT,

        market TEXT,

        odd REAL,

        confidence REAL,

        result TEXT,

        created_at TEXT

    )

    """)

    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    try:

        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )

        

    except Exception as e:

        print("TELEGRAM ERROR")
        print(repr(e))

# =========================================================
# FILTERS
# =========================================================

def blocked_league(league):

    text = league.lower()

    for word in BLOCKED_WORDS:

        if word in text:

            return True

    return False

# =========================================================
# LIVE MATCHES
# =========================================================

def get_live_matches():

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures",

            headers=HEADERS,

            params={
                "live": "all"
            },

            timeout=20

        ).json()

        return r.get(
            "response",
            []
        )

    except:

        return []

# =========================================================
# LIVE STATISTICS
# =========================================================

def get_statistics(fixture_id):

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures/statistics",

            headers=HEADERS,

            params={
                "fixture": fixture_id
            },

            timeout=20

        ).json()

        print(
            "STATS RAW:",
            fixture_id,
            r
        )
        
        return r.get(
            "response",
            []
        )

    except:

        return []

# =========================================================
# ODDS
# =========================================================

def get_odds(fixture_id):

    try:

        r = requests.get(

            f"{BASE_URL}/odds",

            headers=HEADERS,

            params={
                "fixture": fixture_id
            },

            timeout=20

        ).json()

        return r.get(
            "response",
            []
        )

    except:

        return []


# =========================================================
# MATCH ODDS
# =========================================================

def get_match_odds(fixture_id):

    try:

        print(
            "GET ODDS FOR:",
            fixture_id
        )

        odds = get_odds(
            fixture_id
        )

        if not odds:
            return None

        bookmakers = odds[0].get(
            "bookmakers",
            []
        )

        if len(bookmakers) < 3:     

            print(                  
                "WEAK MARKET:",     
                fixture_id          
            )                     

            return None             

        if not bookmakers:
            return None

        bets = bookmakers[0].get(
            "bets",
            []
        )

        for bet in bets:


            if bet.get("name") in [     
                "Both Teams To Score",  
                "BTTS"                   
            ]:                          

                for value in bet.get(    
                    "values",            
                    []                   
                ):                      

                    if value["value"] == "Yes":   

                        print(                    
                            "BTTS ODD =",         
                            value["odd"]          
                        )                                 
         
                              
            if bet.get("name") in [
                "Match Winner",
                "1X2",
                "Winner"
            ]:

                home_odd = None
                draw_odd = None
                away_odd = None

                for value in bet.get(
                    "values",
                    []
                ):

                    print(
                        "VALUE =",
                        value
                    )

                    if value["value"] == "Home":

                        home_odd = float(
                            value["odd"]
                        )

                    elif value["value"] == "Draw":

                        draw_odd = float(
                            value["odd"]
                        )

                    elif value["value"] == "Away":

                        away_odd = float(
                            value["odd"]
                        )             
               

            print(
                "ODDS FOUND:",
                home_odd,
                draw_odd,
                away_odd
            )

            if (
                home_odd is not None
                and
                draw_odd is not None
                and
                away_odd is not None
            ):

                return (
                    home_odd,
                    draw_odd,
                    away_odd
                )

                print(
                    "INCOMPLETE ODDS:",
                    home_odd,
                    draw_odd,
                    away_odd
                )

        return None

    except Exception as e:

        print(
            "GET MATCH ODDS ERROR:",
            repr(e)
        )

        return None



# =========================================================
# EXTRACT STAT
# =========================================================

def extract(team, stat_name):

    try:

        for stat in team["statistics"]:

            if stat["type"] == stat_name:

                value = stat["value"]

                if value is None:
                    return 0

                if isinstance(value, str):

                    value = value.replace("%", "")

                    try:
                        value = int(value)

                    except:

                        return 0

                return value

    except:

        pass

    return 0


# =========================================================
# PRESSURE ENGINE
# =========================================================

def calculate_pressure(team):

    pressure = 0

    possession = extract(
        team,
        "Ball Possession"
    )

    shots_on = extract(
        team,
        "Shots on Goal"
    )

    total_shots = extract(
        team,
        "Total Shots"
    )

    corners = extract(
        team,
        "Corner Kicks"
    )

    attacks = extract(
        team,
        "Dangerous Attacks"
    )

    if shots_on == 0 and attacks < 35:
        return 0

 
    # possession

    if possession >= 55:
        pressure += 8

    if possession >= 60:
        pressure += 10

    if possession >= 65:
        pressure += 12

    # shots on target

    if shots_on >= 3:
        pressure += 18

    if shots_on >= 5:
        pressure += 18

    if shots_on >= 7:
        pressure += 25

    # total shots

    if total_shots >= 8:
        pressure += 8

    if total_shots >= 12:
        pressure += 10

    if total_shots >= 16:
        pressure += 12

    # corners

    if corners >= 4:
        pressure += 6

    if corners >= 7:
        pressure += 8

    if corners >= 10:
        pressure += 10

    # dangerous attacks

    if attacks >= 15:
        pressure += 18

    if attacks >= 25:
        pressure += 18

    if attacks >= 35:
        pressure += 12

    return min(
        pressure,
        100
    )
 

# =========================================================
# UPCOMING MATCHES
# =========================================================

def get_upcoming_matches():

    matches = []

    now = datetime.now(TZ)

    for i in range(2):

        date = (
            now + timedelta(days=i)
        ).strftime("%Y-%m-%d")

        try:

            r = requests.get(

                f"{BASE_URL}/fixtures",

                headers=HEADERS,

                params={
                    "date": date
                },

                timeout=20

            ).json()

            for match in r.get(
                "response",
                []
            ):

                fixture_time = datetime.fromisoformat(
                    match["fixture"]["date"].replace(
                        "Z",
                        "+00:00"
                    )
                )

                fixture_time = fixture_time.astimezone(
                    TZ
                )

                hours_left = (
                    fixture_time - now
                ).total_seconds() / 3600

                if 0 <= hours_left <= 6:

                    matches.append(
                        match
                    )

        except:

            pass

    matches.sort(

        key=lambda x:
        x["fixture"]["date"]

    )

    return matches


# =========================================================
# CARD PRESSURE
# =========================================================

def calculate_card_pressure(          

    minute,                           

    home_fouls,                       
    away_fouls,                       

    home_yellow,                    
    away_yellow,                    

    home_red,                       
    away_red,                       

    home_danger,                      
    away_danger                      

):                                    

    pressure = 50                     

    total_fouls = (                  

        home_fouls                    
        +                            
        away_fouls                   

    )                                 

    total_yellow = (                  

        home_yellow                   
        +                            
        away_yellow                   

    )                                

    total_red = (                     

        home_red                     
        +                             
        away_red                     

    )                                

    total_danger = (                  

        home_danger                  
        +                            
        away_danger                   

    )                                 


    pressure += min(                 

        20,                          

        total_fouls                  

    )                               


    pressure += min(                

        18,                          

        total_yellow                  
        *                           
        6                             

    )                                


    pressure += min(                 

        10,                           

        total_red                    
        *                           
        5                             

    )                                 


    pressure += min(                  

        15,                         

        total_danger                  
        //                            
        10                            

    )                                


    if minute >= 70:                  

        pressure += 10               

    elif minute >= 55:                

        pressure += 5                


    return min(                       

        95,                          

        pressure                     

    )                                

             
# =========================================================
# LIVE ANALYSIS
# =========================================================

def analyze_live_match(fixture):
    try:
        fixture_id = fixture["fixture"]["id"]

        minute = fixture["fixture"]["status"]["elapsed"]
        
        home_goals = fixture["goals"].get("home", 0) or 0
        away_goals = fixture["goals"].get("away", 0) or 0
        current_goals = home_goals + away_goals
        

        home_team = fixture["teams"]["home"]["name"]
        away_team = fixture["teams"]["away"]["name"]


        country = fixture["league"]["country"]    

        if (                                      

            country in [                          

                "Russia",                          

                "Belarus"                          

            ]                                      

        ):                                        

            return None                            


        banned = [

            "russia",
            "belarus"

        ]

        check_text = (
            home_team +
            " " +
            away_team
        ).lower()

        for word in banned:

            if word in check_text:
                return None

        text = (
            home_team +
            " " +
            away_team
        ).lower()

        blocked = [

            "res",
            "reserve",

            "women",

            "u17",
            "u18",
            "u19",
            "u20",
            "u21",
            "u22",
            "u23"

        ]

        for word in blocked:

            if word in text:
                return None

        stats = get_statistics(
            fixture_id
        )
         

        print(                       
            "LIVE STATS:",          
            fixture_id,              
            len(stats)               
        )                           

        if len(stats) < 2:

            print(
                "NO STATS:",
                fixture_id
            )

            return None         

        home_stats = stats[0]
        away_stats = stats[1]

        home_red = extract(
            home_stats,
            "Red Cards"
        )

        away_red = extract(
            away_stats,
            "Red Cards"
        )

        home_pressure = calculate_pressure(
            home_stats
        )

        away_pressure = calculate_pressure(
            away_stats
        )

        # FORM BONUS

        home_form = get_team_form(
            fixture["teams"]["home"]["id"],
            venue="home"
        )

        away_form = get_team_form(
            fixture["teams"]["away"]["id"],
            venue="away"
        )

        if home_form:

           home_pressure += min(
               12,
               round(home_form["form_pct"] / 10)
        )

        if away_form:

            away_pressure += min(
            15,
            round(away_form["form_pct"] / 8)
        )

        home_pressure = min(
            home_pressure,
            100
        )

        away_pressure = min(
            away_pressure,
            100
        )


        home = fixture["goals"]["home"] or 0     
        away = fixture["goals"]["away"] or 0      

        goal_diff = abs(                         

            home - away                           

        )                                         


        # GAME STATE ENGINE           

        if goal_diff >= 2:             

            if home > away:               

                if minute >= 60:          

                    home_pressure -= 10   
                    away_pressure += 6   

                if minute >= 75:          

                    home_pressure -= 5    

            elif away > home:             

                if minute >= 60:          

                    away_pressure -= 10   
                    home_pressure += 6    

                if minute >= 75:         

                    away_pressure -= 5    

        elif goal_diff == 1:             

            if home > away:             

                if minute >= 70:         

                    home_pressure -= 4  
                    away_pressure += 4  

            elif away > home:            

                if minute >= 70:         

                    away_pressure -= 4   
                    home_pressure += 4   
     

        if home_form and home_form["avg_scored"] < 0.9:

            home_pressure -= 8

        if away_form and away_form["avg_scored"] < 0.9:

            away_pressure -= 8

        if (                              
            away_form                     
            and                            
            away_form["avg_conceded"] >= 1.5   
        ):                                

            home_pressure += 5             


        if (                               
            home_form                     
            and                            
            home_form["avg_conceded"] >= 1.5  
        ):                                 

            away_pressure += 5             
         
     
        if home_red > away_red:

            home_pressure -= 25
            away_pressure += 15

        home_xg = extract(
            home_stats,
            "Expected Goals"
        )

        away_xg = extract(
            away_stats,
            "Expected Goals"
        )

        if home_xg >= 1.2:

            home_pressure += 10

        elif home_xg >= 0.8:

            home_pressure += 5

        if away_xg >= 1.2:

            away_pressure += 10

        elif away_xg >= 0.8:

           away_pressure += 5

        if away_red > home_red:

            away_pressure -= 25
            home_pressure += 15

        home_shots_on = extract(
            home_stats,
            "Shots on Goal"
        )

        away_shots_on = extract(
            away_stats,
            "Shots on Goal"
        )

        if minute >= 35 and home_shots_on == 0:
  
            home_pressure -= 10

        if away_shots_on == 0:

            away_pressure -= 10

        if home_shots_on == 0:

            away_pressure += 5

        if away_shots_on == 0:

            home_pressure += 5
          
        home_total_shots = extract(
            home_stats,
            "Total Shots"
        )

        away_total_shots = extract(
            away_stats,
            "Total Shots"
        )

        if home_shots_on >= 6:
            home_pressure += 5

        if away_shots_on >= 6:
            away_pressure += 5
        
        home_corners = extract(
            home_stats,
            "Corner Kicks"
        )

        away_corners = extract(
            away_stats,
            "Corner Kicks"
        )   

        home_fouls = extract(                 
            home_stats,                       
            "Fouls"                           
        )                                    

        away_fouls = extract(                  
            away_stats,                       
            "Fouls"                            
        )                                   

        home_yellow = extract(                
            home_stats,                        
            "Yellow Cards"                    
        )                                    

        away_yellow = extract(                
            away_stats,                       
            "Yellow Cards"                     
        )                                    

        print(                                
            "CARD STATS:",                    
            home_fouls,                       
            away_fouls,                      
            home_yellow,                      
            away_yellow                       
        )                                    

        shots_diff = abs(
            home_shots_on -
            away_shots_on
        )

        corners_diff = abs(
            home_corners -
            away_corners
        )

        dominance = abs(
            home_pressure -
            away_pressure
        )             


        print(
            "LIVE MINUTE:",
            fixture_id,
            minute
        )

        if not minute:
            return None

        if minute < 25:
            return None

        # 🚩 FIRST HALF OVER 1.5 CORNERS            

        first_half_corner = False         

        if (                               

            35 <= minute <= 45              
            and                             

            max(                            

                home_pressure,             
                away_pressure               

            ) >= 78                        
            and                            

            max(                            

                home_shots_on,             
                away_shots_on              

            ) >= 4                          
            and                             

            (                             

                home_corners              
                +                          
                away_corners              

            ) <= 7                        

        ):                                 

            first_half_corner = True        

            print(                         

                "FIRST HALF CORNER MODE",   

                fixture_id,                

                minute,                     

                home_corners,              

                away_corners                

            )                             
     

        print(
            "PASSED MINUTE:",
            fixture_id
        )

        if minute > 90:
            return None

        # CARD PRESSURE                     

        card_probability = (             

            calculate_card_pressure(      

                minute,                   

                home_fouls,               
                away_fouls,                

                home_yellow,              
                away_yellow,              

                home_red,                 
                away_red,                 

                home_pressure,            
                away_pressure            

            )                             

        )                                


        print(                           

            "CARD PROB:",                

            card_probability,             

            home_fouls,                   
            away_fouls,                   

            home_yellow,                 
            away_yellow                   

        )                               


        if (                             

            minute >= 55                
            and                          

            card_probability >= 82       
            and                          

            (                            

                home_yellow             
                +                         
                away_yellow              

            ) >= 3                       
            and                          

            (                            

                home_fouls               
                +                        
                away_fouls               

            ) >= 20                      

        ):                               

            return (    
             
                "🟨 OVER 1.5 NEXT CARDS", 

                88,                       

                minute,                   

                card_probability          

            )                           


        best_pressure = max(         

            home_pressure,            

            away_pressure             

        )                            

        minimum_pressure = 50        

        if minute >= 60:             

            minimum_pressure = 54     

        if minute >= 70:             

            minimum_pressure = 57     

        if (                         

            best_pressure             

            <                         

            minimum_pressure         

        ):                            

            return None               

        if dominance < 7:            

            return None               

        min_shots = 4               

        if minute >= 60:              

            min_shots = 5             

        if minute >= 70:            

            min_shots = 5            
     

        home = fixture["goals"]["home"] or 0     
        away = fixture["goals"]["away"] or 0     

        total = home + away                      

        if total >= 5:                            
            min_shots -= 1                        

        if (                                     

            max(                                  

                home_shots_on,                   

                away_shots_on                    

            )                                    

            <                                     

            min_shots                            

        ):                                       

            return None                           

        goal_diff = abs(                         

            home - away                           

        )     


        # CARD BONUS ENGINE              

        if goal_diff <= 1:               

            card_probability += 4        

        if (                            

            home == away                 

        ):                              

            card_probability += 4       

        if minute >= 75:                

            card_probability += 4       

        if (                            

            home_pressure >= 75         

            and                          

            away_pressure >= 75          

        ):                               

            card_probability += 5        

        if (                            

            (                            

                home_fouls               

                +                        

                away_fouls               

            )                            

            >=                           

            28                           

        ):                              

            card_probability += 5       

        if (                             

            (                            

                home_yellow              

                +                        

                away_yellow              

            )                           

            >=                           

            5                            

        ):                              

            card_probability += 5        

        card_probability = min(         

            95,                          

            card_probability            

        )      


        # EXTREME CARD MODE              

        if (                             

            minute >= 80                 

            and                          

            card_probability >= 85       

            and                          

            (                            

                home_pressure            

                +                        

                away_pressure            

            )                            

            >=                           

            140                          

            and                          

            (                            

                home_fouls               

                +                        

                away_fouls               

            )                            

            >=                           

            26                           

            and                          

            (                            

                home_yellow              

                +                        

                away_yellow              

            )                            

            >=                           

            4                            

        ):                               

            return (                     

                "🟨 OVER 1.5 NEXT CARDS", 

                92,                      

                minute,                  

                92                       

            )                           
     

        # FAST GOALS OVERRIDE

        if (
            minute <= 40
            and
            total >= 2
            and
            goal_diff >= 2
            and
            max(
                home_pressure,
                away_pressure
            ) >= 80
        ):

            if home > away:

                return (

                    "🎯 NEXT GOAL HOME",
                    90,
                    minute,
                    90

                )

            else:

                return (

                    "🎯 NEXT GOAL AWAY",
                    90,
                    minute,
                    90

                )         
                   

        print(
            "OVER15 CHECK:",
            home_team,
            away_team,
            minute,
            home_pressure,
            away_pressure,
            home_shots_on,
            away_shots_on,
            home_corners,
            away_corners
        )       


                # NORMAL NEXT GOAL

        if (                              

            minute > 40                  

            and

            minute < 75                     

            and

            max(                           

                home_pressure,            

                away_pressure              

            ) >= 65                        

            and

            max(                            

                home_shots_on,              

                away_shots_on              

            ) >= 4                         

        ):                                  

            if home_pressure > away_pressure:    

                return (                        

                    "🎯 NEXT GOAL HOME",         

                    min(                         

                        95,                      

                        home_pressure           

                    ),                          

                    minute,                     

                    min(                       

                        95,                     

                        home_pressure            

                    )                           

                )                                

            elif away_pressure > home_pressure:  

                return (                         

                    "🎯 NEXT GOAL AWAY",         

                    min(                        

                        95,                      

                        away_pressure            

                    ),                           

                    minute,                     

                    min(                        

                        95,                      

                        away_pressure           

                    )                           

                )                                

        # OVER 1.5 REMAINING GOALS    

        if (                            

            minute <= 75                

            and                        

            max(                        

                home_pressure,          

                away_pressure           

            ) >= 55                     

            and                         

            (                           

                home_shots_on           

                +                       

                away_shots_on           

            ) >= 4                      

            and                         

            (                           

                home_corners            

                +                       

                away_corners            

            ) >= 3                      

        ):                             

            return (                   

                "🚀 OVER 1.5 REMAINING GOALS", 

                90,                     

                minute,                 

                90                    
                
            )                           
                    

        # OVER 1.5 NEXT CORNERS          

        corner_probability = 50        

        corner_probability += (        

            max(                        

                home_pressure,         

                away_pressure           

            ) - 70                      

        ) * 2                           

        corner_probability += (         

            home_corners               

            +                          

            away_corners                

        )                               

        corner_probability += (         

            shots_diff                  

            *                           

            2                          

        )                               

        corner_probability = min(       

            95,                        

            max(                        

                50,                    

                corner_probability      

            )                          

        )        


        print(
            "CORNER CHECK:",
            home_team,
            away_team,
            minute,
            corner_probability,
            home_corners,
            away_corners
        )

        if (                           

            minute >= 60               

            and                        

            minute <= 88               

            and                        

            (                         

                home_corners          

                +                       

                away_corners           

            ) >= 5                      

            and                       

            (                          

                home_total_shots        

                +                       

                away_total_shots      

            ) >= 8                    

            and                        

            max(                       

                home_pressure,         

                away_pressure           

            ) >= 65                    

            and                         

            corner_probability >= 65    

        ):                             

            return (                    

                "🚩 OVER 1.5 NEXT CORNERS",   

                corner_probability,     

                minute,                

                corner_probability      

            )          

        print(
            "PASSED CORNERS BLOCK:",
            home_team,
            away_team,
            minute
        )


                # LATE GOAL                   

        if (                           

            minute >= 75               

            and                       

            minute <= 90               

            and                        

            max(                       

                home_pressure,        

                away_pressure          

            ) >= 55                    

            and                       

            (                          

                home_total_shots       

                +                      

                away_total_shots      

            ) >= 8                   

            and                        

            (                          

                home_corners           

                +                      

                away_corners          

            ) >= 5


            and                       

            (                         

                home_xg >= 1.2        

                or                    

                away_xg >= 1.2         

            )                         

        ):                            

            return (                   

            "🔥 LATE GOAL",        

            90,                     

            minute,                 

            90                     

        )                          

    except:                         

        return None                 


# =========================================================
# TEAM FORM
# =========================================================

def get_team_form(team_id, venue=None): 

    cache_key = f"{team_id}_{venue}"

    if cache_key in team_form_cache:

        cache_time, data = team_form_cache[cache_key]

        if time.time() - cache_time < 21600:
            return data

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures",

            headers=HEADERS,

            params={
                "team": team_id,
                "last": 10
            },

            timeout=20

        ).json()

        games = r.get(
            "response",
            []
        )

        if not games:
            return None

        filtered_games = []

        for g in games:

            home_id = g["teams"]["home"]["id"]

            if venue == "home":

                if home_id == team_id:
                    filtered_games.append(g)

            elif venue == "away":

                if home_id != team_id:
                    filtered_games.append(g)

            else:

                filtered_games.append(g)

        games = filtered_games

        if len(games) < 3:
            return None

        scored = 0
        conceded = 0

        wins = 0
        losses = 0
        draws = 0
        home_wins = 0        
        away_wins = 0       

        home_games = 0       
        away_games = 0        
        clean_sheets = 0  
        scored_games = 0         
        over25 = 0
        btts = 0

        for g in games:

            home_id = g["teams"]["home"]["id"]

            gh = g["goals"]["home"] or 0
            ga = g["goals"]["away"] or 0

            if team_id == home_id:

                team_goals = gh
                opp_goals = ga

            else:

                team_goals = ga
                opp_goals = gh

            scored += team_goals
            conceded += opp_goals

            if team_goals > 0:       
                scored_games += 1     

            if opp_goals == 0:       
                clean_sheets += 1     

            if team_goals > opp_goals:     

                wins += 1                 
            
                if team_id == home_id:      
            
                    home_wins += 1         
            
                else:                       
            
                    away_wins += 1         

            elif team_goals < opp_goals:
                losses += 1

            else:
                draws += 1

            if (gh + ga) >= 3:
                over25 += 1

            if gh > 0 and ga > 0:
                btts += 1

        recent_games = games[:5]      

        recent_points = 0             
        recent_over25 = 0    
        recent_scored = 0            
        recent_conceded = 0
        recent_goal_diff = 0  
        weights = [5, 4, 3, 2, 1]

        
        max_weight_points = 0

             
        for i, g in enumerate(recent_games):    

            home_id = g["teams"]["home"]["id"]  

            gh = g["goals"]["home"] or 0        
            ga = g["goals"]["away"] or 0        

            if team_id == home_id:              

                team_goals = gh                 
                opp_goals = ga                  
                home_games += 1                 

            else:                              

                team_goals = ga                  
                opp_goals = gh                  
                away_games += 1                  

            recent_scored += team_goals         
            recent_conceded += opp_goals        

            recent_goal_diff += (                
                team_goals                       
                -                               
                opp_goals                        
            )                                    

            weight = weights[i]                  

            max_weight_points += (             
                weight                          
                *                              
                3                             
            )                                   

            if team_goals > opp_goals:          

                recent_points += (            
                    3                          
                    *                          
                    weight                     
                )                              

            elif team_goals == opp_goals:      

                recent_points += (             
                    1                          
                    *                        
                    weight                     
                )                               

            if (gh + ga) >= 3:                

                recent_over25 += 1                                          

        recent_form_pct = round(            

            (                              

                recent_points             

                /                         

                max_weight_points          

            ) * 100,                        

            2                              

        ) if max_weight_points > 0 else 0  

        print(                            

            "RECENT FORM:",                 

            recent_points,                  

            max_weight_points,             

            recent_form_pct               

        )                                 
               

        recent_avg_scored = round(     
            recent_scored / len(recent_games),
            2
)                                

        recent_avg_conceded = round(     
            recent_conceded / len(recent_games),
            2
)                                

        total = len(games)

        points = wins * 3

        form_pct = round(
            (points / (total * 3)) * 100,
            2
        )

        momentum = round(
            recent_form_pct
            -
            form_pct,
            2
        )

        unbeaten = wins + draws

        unbeaten_pct = round(
            (unbeaten / total) * 100,
            2
        )

        clean_sheet_pct = round(     
            (clean_sheets / total) * 100, 
            2                        
        )      

        scored_pct = round(           
            (scored_games / total) * 100, 
            2                        
        )                            

        goal_diff = (            
            scored              
            -               
            conceded            
        )                     

        result = {

            "home_wins":           
                home_wins,          

            "away_wins":           
                away_wins,          

            "home_games":          
                home_games,         

            "away_games":          
                away_games,        
         
            "scored_pct":           
                 scored_pct,       

            "clean_sheet_pct":      
                clean_sheet_pct,    

            "momentum":           
                momentum,       

            "avg_scored":
                round(scored / total, 2),

            "total_scored":
                scored,

            "goal_diff":
                goal_diff,

            "recent_goal_diff":        
                recent_goal_diff,      

            "avg_conceded":
                round(conceded / total, 2),

            "wins":
                wins,

            "losses":
                losses,

            "draws":
                draws,

            "unbeaten":
                unbeaten,

            "unbeaten_pct":
                unbeaten_pct,

            "over25":                   
                over25,                 

            "over25_pct":               
                round(                   
                    (over25 / total) * 100, 
                    2                   
                ),                      

            "btts":                     
                btts,                    

            "played":                   
                total,                  

            "form_pct":
                form_pct,
         
            "recent_form_pct":
                recent_form_pct,

            "recent_avg_scored":
                recent_avg_scored,

            "recent_avg_conceded":
                recent_avg_conceded,

            "recent_over25":
                recent_over25,
        }

        team_form_cache[cache_key] = (
            time.time(),
            result
        )

        return result

    except:

        return None
    

# =========================================================
# POISSON
# =========================================================

def poisson_over25(home_attack, away_attack):

    

    prob = 0

    for h in range(8):

        for a in range(8):

            total = h + a

            p = (
                poisson.pmf(
                    h,
                    home_attack
                )
                *
                poisson.pmf(
                    a,
                    away_attack
                )
            )

            if total >= 3:

                prob += p

    return round(
        prob * 100,
        2
    )

# =========================================================
# BTTS POISSON
# =========================================================

def poisson_btts(home_attack, away_attack):

    prob = 0

    for h in range(8):

        for a in range(8):

            p = (
                poisson.pmf(
                    h,
                    home_attack
                )
                *
                poisson.pmf(
                    a,
                    away_attack
                )
            )

            if h > 0 and a > 0:

                prob += p

    return round(
        prob * 100,
        2
    )
 
# =========================================================
# FORM SCORE
# =========================================================

def calculate_form_score(            

    home_form,                       
    away_form                        

):                                   

    score = 0                       

    score += (                      
        home_form["form_pct"]        
        +                            
        away_form["form_pct"]        
    ) * 0.20                        

    score += (                      
        home_form["recent_form_pct"] 
        +
        away_form["recent_form_pct"]
    ) * 0.25                         

    score += (                      
        home_form["over25_pct"]     
        +                           
        away_form["over25_pct"]      
    ) * 0.15                        

    score += (                       
        home_form["btts"]            
        +                           
        away_form["btts"]           
    ) * 1.5                         

    score += (                      
        home_form["recent_avg_scored"] 
        +                             
        away_form["recent_avg_scored"] 
    ) * 8                             

    score += (                     
        home_form["scored_pct"]     
        +                           
        away_form["scored_pct"]      
    ) * 0.10                       

    return min(                      
        100,                        
        round(score, 2)             
    )                              
    
# =========================================================
# HOME WIN SCORE
# =========================================================

def home_win_score(

    home_form,
    away_form

):

    score = 0

    score += (
        home_form["total_scored"]
        -
        away_form["total_scored"]
    ) * 0.5

    score += (
        home_form["goal_diff"]
        -
        away_form["goal_diff"]
    ) * 0.5

    score += (                          
        home_form["recent_goal_diff"]   
        -                              
        away_form["recent_goal_diff"]  
    ) * 0.7                            

    score += (
        away_form["losses"]
        -
        home_form["losses"]
    ) * 2

    score += (
        home_form["form_pct"]
        -
        away_form["form_pct"]
    )* 0.4

    score += (                         
        home_form["recent_form_pct"]    
        -                               
        away_form["recent_form_pct"]   
    ) * 0.3    

    score += (                     
        home_form["momentum"]       
        -                           
        away_form["momentum"]   
    ) * 0.4                        

    score += (
        home_form["unbeaten_pct"]
        -
        away_form["unbeaten_pct"]
    ) * 0.1

    score += (
        home_form["avg_scored"]
        -
        away_form["avg_scored"]
    ) * 8

    score += (                               
        home_form["recent_avg_scored"]       
        -                                    
        away_form["recent_avg_scored"]        
    ) * 6                                   

    score += (
        away_form["avg_conceded"]
        -
        home_form["avg_conceded"]
    ) * 5

    score += (                              
        away_form["recent_avg_conceded"]     
        -                                   
        home_form["recent_avg_conceded"]      
    ) * 4           

    print(                       

        "SCORE PARTS:",          

        home_form["total_scored"] - away_form["total_scored"],     

        home_form["goal_diff"] - away_form["goal_diff"],           

        home_form["recent_goal_diff"] - away_form["recent_goal_diff"], 

        home_form["form_pct"] - away_form["form_pct"],             

        home_form["recent_form_pct"] - away_form["recent_form_pct"], 

        home_form["momentum"] - away_form["momentum"]               

    )                         

    return round(score, 2)

# =========================================================
# TEAM STRENGTH
# =========================================================

def team_strength(                    

    form                            

):                                  

    score = 0                        

    score += form["form_pct"] * 0.25            

    score += form["recent_form_pct"] * 0.25     

    score += form["unbeaten_pct"] * 0.10     

    score += form["clean_sheet_pct"] * 0.08  

    score += form["avg_scored"] * 10             

    score += form["recent_avg_scored"] * 12      

    score += form["over25_pct"] * 0.08          

    score -= form["avg_conceded"] * 8           

    score -= form["recent_avg_conceded"] * 10    

    return round(              
        score,                    
        2                        
    )         

# =========================================================
# H2H SCORE
# =========================================================

def h2h_score(                        

    home_id,                           
    away_id                            

):                                     

    try:                               

        r = requests.get(             

            f"{BASE_URL}/fixtures/headtohead",

            headers=HEADERS,

            params={                   

                "h2h":                 
                    f"{home_id}-{away_id}",

                "last": 5             

            },

            timeout=20                

        ).json()                     

        games = r.get(                
            "response",
            []
        )

        score = 0                     

        for i, g in enumerate(games):        

            gh = g["goals"]["home"] or 0      
            ga = g["goals"]["away"] or 0     

            hid = g["teams"]["home"]["id"]   

            weight = 2 if i < 3 else 1           

            hid = g["teams"]["home"]["id"]    

            if hid == home_id:        

                if gh > ga:           

                    score += weight   

                elif gh < ga:         

                    score -= weight    

            else:                   

                if ga > gh:          

                    score += weight  

                elif ga < gh:         

                    score -= weight       

        return score                 

    except:                           

        return 0     

# =========================================================
# ODDS SCORE
# =========================================================

def odds_score(               

    probability,              
    odd                       

):                           

    try:                      

        implied = (           
            100               
            /                
            odd              
        )                     

        edge = (              
            probability      
            -                 
            implied           
        )                   

        return max(           
            -20,              
            min(              
                20,           
                edge          
            )                 
        )                     

    except:                  

        return 0            

    
# =========================================================
# LEAGUE WEIGHT
# =========================================================

TOP_GOAL_COUNTRIES = [

    "Netherlands",
    "Norway",
    "Sweden",
    "Denmark",
    "Belgium",
    "Austria"

]

LOW_GOAL_COUNTRIES = [

    "Peru",
    "Paraguay",
    "Bolivia",
    "Ecuador",
    "Venezuela"

]

def league_score(country, market):

    score = 0

    if country in TOP_GOAL_COUNTRIES:

        if market == "⚽ OVER 2.5":
            score += 10

        elif market == "💎 BTTS":
            score += 8

    if country in LOW_GOAL_COUNTRIES:

        if market == "⚽ OVER 2.5":
            score -= 10

        elif market == "💎 BTTS":
            score -= 8

    return score

# =========================================================
# FAIR ODDS
# =========================================================

def fair_odds(probability):

    if probability <= 0:
        return 999

    return round(
        100 / probability,
        2
    )

# =========================================================
# VALUE
# =========================================================

def value_edge(                    

    probability,                   

    odd                            

):                                 

    try:                           

        market_prob = (            

            100                    

            /                      

            odd                    

        )                           

        edge = (                    

            (

                probability          

                /                  

                100                  

            )                       

            *

            odd                     

            -                        

            1                       

        ) * 100                      

        edge += (                  

            probability              

            -                        

            market_prob              

        ) * 0.25                    

        return round(               

            edge,                   

            2                       

        )                            

    except:                         

        return 0                    

# =========================================================
# NO VIG
# =========================================================

def no_vig_probabilities(          

    home_odd,                     
    draw_odd,                     
    away_odd                       

):                                 

    try:                           

        home = (                   

            1                      
            /                      
            home_odd               

        )                         

        draw = (                  

            1                     
            /                      
            draw_odd               

        )                          

        away = (                   

            1                      
            /                      
            away_odd               

        )                          
     

        total = (                  

            home                   
            +                      
            draw                   
            +                      
            away                   

        )                          

        return (                   

            round(                 

                home               
                /                  
                total              
                *                 
                100,               
                2                  

            ),                     

            round(                 

                draw               
                /                  
                total              
                *                  
                100,               
                2                 

            ),                     

            round(                 

                away               
                /                  
                total              
                *                  
                100,               
                2                  

            )                      

        )                          

    except:                        

        return None                


    
# =========================================================
# SAVE SIGNAL
# =========================================================

def save_signal(

    fixture_id,
    country,
    league,

    home,
    away,

    market,

    odd,
    confidence

):

    conn = sqlite3.connect(
        "v3_ai.db"
    )

    cur = conn.cursor()

    cur.execute(

        """
        INSERT INTO signals (

            fixture_id,

            country,
            league,

            home_team,
            away_team,

            market,

            odd,
            confidence,

            created_at

        )

        VALUES (?,?,?,?,?,?,?,?,?)
        """,

        (

            fixture_id,

            country,
            league,

            home,
            away,

            market,

            odd,
            confidence,

            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        )

    )

    conn.commit()
    conn.close()

# =========================================================
# PREMATCH SCORE
# =========================================================

def calculate_final_score(

    form_score,
    poisson_score,

    value_score,
    league_bonus

):

    score = (

        form_score * 0.25 +

        poisson_score * 0.55 +

        value_score * 0.15 +

        league_bonus * 0.05

    )

    return round(score, 2)



# =========================================================
# CONFIDENCE
# =========================================================

def confidence_from_score(score):     

    if (                              

        score >= 85                    

    ):                                 

        return 90                    

    elif (                           

        score >= 70                    

    ):                                 

        return 80                     

    elif (                            

        score >= 55                    

    ):                                

        return 70                     

    elif (                             

        score >= 40                   

    ):                                 

        return 60                     

    return 50                                     
# =========================================================
# PREMATCH ANALYSIS
# =========================================================

def analyze_prematch_match(match):

    try:

        fixture_id = match["fixture"]["id"]
        match_odds = get_match_odds(
            fixture_id
        )

      
         
        if not match_odds:
            return None

        if (
           match_odds[0] is None
           or
           match_odds[2] is None
       ):
           return None

        market_prob = no_vig_probabilities(     

            match_odds[0],                      
            match_odds[1],                      
            match_odds[2]                      

        )                                       

        if market_prob:                         

            market_home = market_prob[0]        
            market_draw = market_prob[1]        
            market_away = market_prob[2]        

            print(                              

                "NO VIG:",                       

                market_home,                    
                market_draw,                    
                market_away                      

            )                                  
      

        country = match["league"]["country"]
        league = match["league"]["name"]

        if country in [

            "Russia",
            "Belarus"

        ]:
            return None

        bad_words = [

            "u17",
            "u18",
            "u19",
            "u20",
            "u21",
            "u23",

            "women",

            "reserve",
            "reserves",

            "friendly",

            "russia",
            "russian",

            "belarus",
            "belarusian",

        ]

        league_text = (
            country +
            " " +
            league
        ).lower()

        for word in bad_words:

            if word in league_text:
                return None

        if country in BAD_COUNTRIES:
            return None

        if blocked_league(league):
            return None

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        if home.endswith(" W"):
            return None

        if away.endswith(" W"):
            return None

        if " II" in home:
            return None

        if " II" in away:
            return None

        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]

        home_form = get_team_form(
            home_id,
            venue="home"
        )

        away_form = get_team_form(
            away_id,
            venue="away"
        )
        if not home_form or not away_form:
            return None

        if (
            home_form["played"] < 3
            or
            away_form["played"] < 3
        ):
            return None

        if (
            away_form["avg_scored"] < 0.8
            and
            home_form["avg_scored"] < 1.0
        ):
            return None
        print(                 
            "OVER ANALYZE:",    
            home,               
            away                
        )                       

        home_attack = (                             
            home_form["avg_scored"]                 
            +                                        
            away_form["avg_conceded"]                
        ) / 2      

        home_attack += (                       

            home_form["recent_avg_scored"]    

            -                                

            home_form["avg_scored"]          

        ) * 0.20                             

        away_attack = (                              
            away_form["avg_scored"]                 
            +                                        
            home_form["avg_conceded"]                
        ) / 2  

        away_attack += (                     

            away_form["recent_avg_scored"]     

            -                              

            away_form["avg_scored"]           

        ) * 0.20                             

        expected_goals = (         
            
            home_attack                 
            +                              
            away_attack                    
        )            


        # HOT ATTACK BONUS                

        if (                               

            home_form["recent_avg_scored"] 

            >=                             

            home_form["avg_scored"] + 0.5  

        ):                                

            expected_goals += 0.15        

        if (                               

            away_form["recent_avg_scored"]

            >=                            

            away_form["avg_scored"] + 0.5 

        ):                                

            expected_goals += 0.15         

        over_prob = poisson_over25(    

            home_attack,                             
            away_attack                              

        )       


        # BOTH DEFENCES WEAK BONUS         

        if (                              

            home_form["avg_conceded"] >= 1.5  

            and                            

            away_form["avg_conceded"] >= 1.5 

        ):                                

            over_prob += 2                

        over_prob = min(                  

            95,                           

            over_prob                     

        )                                


        # HIGH SCORING FORM BONUS        

        if (                              

            home_form["recent_avg_scored"] 

            >= 2.0                        

            and                           

            away_form["recent_avg_scored"]

            >= 1.5                         

        ):                               

            over_prob += 1                 

        over_prob = min(                  

            95,                           

            over_prob                     

        )                                

        btts_home_attack = (                
            home_form["avg_scored"]          
            +                                
            away_form["avg_conceded"]       
        ) / 2                              
        
        btts_away_attack = (                 
            away_form["avg_scored"]         
            +                                
            home_form["avg_conceded"]        
        ) / 2                               
        
        btts_prob = poisson_btts(           
        
            btts_home_attack,               
            btts_away_attack                 
        
        )   


        # BTTS MOMENTUM BONUS              

        if (                              

            home_form["recent_avg_scored"] >= 1.6  

            and                            

            away_form["recent_avg_scored"] >= 1.3  

        ):                                

            btts_prob += 3                

        btts_prob = min(                  

            95,                           

            btts_prob                     

        )                                

        form_score = calculate_form_score(
            home_form,
            away_form
        )
        
        print(                  
            "SIGNALS START:",  
            home,              
            away               
        )                       
     
        signals = []
        
        home_odds_ok = False
        away_odds_ok = False
        
                # HOME WIN

        home_strength = team_strength(      
            home_form                     
        )      

             

        away_strength = team_strength(     
            away_form                       
        )     

          

        h2h = h2h_score(             
            home_id,                   
            away_id                   
        )                            

        home_score = home_win_score(       
            home_form,                     
            away_form                       
        )    
           
        home_score *= 0.6

        print(                   

            "HOME RAW SCORE:",    

            home,                  

            away,                 

            home_score            

        )    

         
        strength_gap = (                

            home_strength               

            -                            

            away_strength                

        )                                

        home_score += (                  

            strength_gap                

            *                            

            0.35                       

        )                                

        if (                            

            strength_gap >= 25          

        ):                              

            home_score += 3              

        elif (                           

            strength_gap >= 15           

        ):                              

            home_score += 1       


        # ATTACK / DEFENSE INDEX      

        attack_gap = (                 

            (

                home_form["avg_scored"]       

                +

                home_form["recent_avg_scored"] 

            )                                 

            -

            (

                away_form["avg_conceded"]       

                +

                away_form["recent_avg_conceded"]

            )                                 

        )                                      

        if (                        

            attack_gap >= 1.80       

        ):                           

            home_score += 4          

        elif (                        

            attack_gap >= 1.40        

        ):                           

            home_score += 2         

        elif (                        

            attack_gap >= 1.00       

        ):                            

            home_score += 1          
        debug_base = home_score

        print(                     

            "HOME BASE SCORE:",      
                   
            home,                    

            away,                   

            home_score              

        )                            
       

        home_score += h2h  


        # GOAL DIFFERENCE BONUS       

        goal_diff = (                 

            home_form["avg_scored"]    

            -                          

            home_form["avg_conceded"]  

        )                             

        if (                           

            goal_diff >= 1.40         

        ):                            

            home_score += 4          

        elif (                         

            goal_diff >= 0.90         

        ):                             

            home_score += 2           

        elif (                        

            goal_diff >= 0.50          

        ):                             

            home_score += 1            

        print(
            "AFTER STRENGTH:",
            home_score
        )
                 
        # HOME VENUE BONUS                

        if (                              
        
            home_form["home_games"] >= 3   
        
        ):                                
        
            home_home_winrate = (          
        
                home_form["home_wins"]    
        
                /                        
        
                home_form["home_games"]    
        
            ) * 100                       
        
            if (                           
        
                home_home_winrate >= 70   
        
            ):                            
        
                home_score += 5           
        
            elif (                        
        
                home_home_winrate >= 55    
        
            ):                            
        
                home_score += 3          

        if h2h >= 4:          

            home_score += 2  

        elif h2h >= 2:       

            home_score += 2   

        if home_form["home_games"] > 0:    

            home_score += (                

                (
                    home_form["home_wins"]  
                    /
                    home_form["home_games"]
                )

                * 5                       

            )                               

        if match_odds:                   

            home_edge_score = odds_score( 
                min(95, home_score),      
                match_odds[0]            
            )                                          

            home_score += (              
                home_edge_score           
                *                        
                0.25                       
            )      

        print(
            "HOME EDGE SCORE:",
            home,
            away,
            home_edge_score
        )

        print(            

            "AFTER ODDS:", 

            home_score     

        )                  

     

        print(
            "AFTER VENUE:",
            home_score
        )

                
        # FORM COLLAPSE BONUS

        if (
            away_form["losses"] >= 5
            or
            away_form["form_pct"] <= 35
        ):

            home_score += 6

     

        # GOAL DOMINANCE BONUS          

        if (                           

            home_form["avg_scored"]     
            >=                         
            2.0                         

            and                          

            away_form["avg_conceded"]   
            >=                           
            1.4                         

        ):                               

            home_score += 3       

        # EXPECTED GOALS BONUS         

        if (                            

            expected_goals >= 3.2       

            and                        

            home_form["avg_scored"]    
            >=                          
            1.8                        

            and                        

            away_form["avg_conceded"]   
            >=                         
            1.3                        

        ):                             

            home_score += 4             

        elif (                         

            expected_goals >= 2.8       

        ):                             

            home_score += 2             
     

        # SUPER FORM BONUS                

        if (                             

            home_form["form_pct"] >= 80   

            and                          

            home_form["wins"] >= 6       

        ):                               

            home_score += 3               


        # DEFENSIVE BONUS                

        if (                             

            home_form["clean_sheet_pct"] >= 50  

        ):                               

            home_score += 2               

        elif (                           

            home_form["clean_sheet_pct"] >= 35   

        ):                              

            home_score += 1                     

        print(               

            "AFTER DEFENSE:", 

            home_score        

        )   

        print(
            "AFTER COLLAPSE+DEFENSE:",
            home_score
        )

        home_edge = (
            home_form["wins"]
            -
            away_form["wins"]
        )

        # WIN EDGE BONUS                

        if (                             

            home_edge >= 4               

        ):                               

            home_score += 3             

        elif (                           

            home_edge >= 2              

        ):                              

            home_score += 1              

        form_gap = (                         
            home_form["form_pct"]             
            -
            away_form["form_pct"]             
        )         

        print(
            "AFTER WIN EDGE:",
            home_score
        )

        # MOMENTUM BONUS               

        momentum_gap = (              

            home_form["momentum"]      

            -                         

            away_form["momentum"]     

        )                             

        if (                           

            momentum_gap >= 20         

        ):                             

            home_score += 3           

        elif (                        

            momentum_gap >= 10        

        ):                            

            home_score += 1            

        recent_gap = (                        
            home_form["recent_form_pct"]      
            -
            away_form["recent_form_pct"]      
        )       


        # RECENT FORM EXPLOSION BONUS     
       
        if (                              

            recent_gap >= 35            

        ):                               

            home_score += 5              

        elif (                            
            
            recent_gap >= 25            

        ):                              

            home_score += 3              

        elif (                           

            recent_gap >= 15              

        ):                             

            home_score += 1               

        print(               

            "AFTER RECENT:", 

            home_score      

        )                   


        # RECENT GOAL DIFF BONUS         

        if (                            

            home_form["recent_goal_diff"] >= 6   

        ):                               

            home_score += 3              

        elif (                          

            home_form["recent_goal_diff"] >= 3   

        ):                              

            home_score += 1             

        home_super_value = False
        home_value = False

        if match_odds:

            if (
                match_odds[0] is not None
            ):

                edge = value_edge(              

                    min(95, home_score),        
                
                    match_odds[0]               
                
                )                                       

                print(
                    "HOME VALUE EDGE:",
                    home,
                    away,
                    edge
                )


                print(
                    "HOME VALUE INPUT:",
                    home,
                    away,
                    home_score,
                    match_odds[0]
               )


                if edge >= 12:                 

                    home_super_value = True    
                
                    home_score += 4            
                
                   
                
                elif edge >= 6:                
                
                    home_value = True         
                
                    home_score += 2             

        print(             

            "AFTER VALUE:", 

            home_score      

        )                   
        
        if (
            match_odds
            and
            match_odds[0] is not None
        ):

            home_odds_ok = (
                1.40 <= match_odds[0] <= 2.80
            )
        print(
            "HOME BONUS TOTAL:",
            home,
            away,
            home_score - debug_base
        )       


        bonus_total = (              

            home_score                 

            -                          

            debug_base                 

        )                              

        if (                           

            bonus_total > 30            

        ):                            

            home_score -= (            

                bonus_total             

                -                      

                28                      

            )                         

        home_score = min(          
            80,                    
            max(                  
                -80,               
                home_score        
            )                      
        )                         

    

        print(                 
            "HOME SCORE:",     
            home,               
            away,              
            home_score          
        )                       

        home_probability = max(     
            0,                      
            min(                    
                100,                 
                50 + home_score      
            )                        
        )       


        # QUALITY CONFIRMATION          

        if (                            

            home_form["wins"] >= 5      

            and                          

            home_form["losses"] <= 1     

            and                          

            home_form["form_pct"] >= 70 

            and                         

            home_edge >= 3               

            and                         

            market_home >= 52            

        ):                              

            home_score += 3             

        elif (                           

            home_form["wins"] >= 4       

            and                         

            home_form["form_pct"] >= 65  

            and                          

            home_edge >= 2             

        ):                               

            home_score += 1             

        # STRONG FAVOURITE BONUS        

        if (                            

            home_probability >= 72      

            and                         

            home_edge >= 3             

            and                        

            home_form["wins"] >= 4      

            and                         

            home_form["losses"] <= 1    

        ):                             

            home_score += 4            

        elif (                         

            home_probability >= 68      

            and                         

            home_edge >= 2              

        ):                             

            home_score += 2             


        # CONSENSUS BONUS               

        if (                          

            home_probability >= 70      

            and                         

            home_edge >= 3              

            and                        

            market_home >= 55           

        ):                              

            home_score += 3             

        print(                  
            "HOME PROB:",       
            home,              
            away,               
            home_probability    
        )                      

       

        home_signal = False


        print(
            "HOME FILTERS:",
            home,
            away,
            "score=", home_score,
            "odds_ok=", home_odds_ok,
            "edge=", home_edge,
            "form_gap=", form_gap,
            "recent_gap=", recent_gap,
            "recent_form=", home_form["recent_form_pct"],
            "prob=", home_probability
        )

        # ATTACK / DEFENSE FILTER         

        home_balance = (                  

            home_form["avg_scored"]        

            -                              

            home_form["avg_conceded"]      

        )                                 

        away_balance = (                   

            away_form["avg_scored"]       

            -                             

            away_form["avg_conceded"]      

        )                                 

        if (                               

            home_balance < 0.30           

        ):                                 

            return None          


        # SUPER DOMINANCE FILTER     

        goal_gap = (                

            home_form["avg_scored"]   

            -                       

            away_form["avg_scored"]  

        )                            

        defense_gap = (              

            away_form["avg_conceded"] 

            -                         

            home_form["avg_conceded"] 

        )                             

        dominance_ok = (             

            goal_gap >= 0.40          

            and                       

            defense_gap >= 0.30       

        )       


        # FORM CONSISTENCY           

        consistency_ok = (           

            home_form["wins"]        

            >=                       

            home_form["losses"] * 2   

        )                            
     
        
        if (
            home_score >= 50           
            and
            home_odds_ok
            and
            home_form["unbeaten_pct"] >= 60
            and
            home_form["wins"] >= 2  
            and                         
            home_form["losses"] <= 3      
            and                        
            home_form["draws"] <= 4    
            and                         
            home_edge >= 2                 
            and
            form_gap >= 10
            and
            recent_gap >= 5
            and
            home_form["recent_form_pct"] >= 40                                                              
            and                                   
            home_form["avg_scored"] >= 1.5
            and
            (
                home_form["avg_scored"]
                -
                away_form["avg_scored"]
            ) >= 0.30
            and                           

            (
                home_form["avg_scored"]  

                -

                home_form["avg_conceded"] 

            ) >= 0.30                    
            and
            home_form["recent_avg_scored"] >= 1.5
            and                           
            home_form["recent_goal_diff"] >= 1   
            and
            home_form["avg_conceded"] <= 1.3
            and
            away_form["avg_conceded"] >= 1.2
            and
            away_form["recent_avg_conceded"] >= 1.2
            and
            home_probability >= 65
            and
            dominance_ok
            and
            consistency_ok
        ):

            print(
                "HOME SIGNAL:",
                home_score                
            )

            signals.append(              

                (                          

                    "🏆 HOME WIN",          

                    confidence_from_score(  
                        home_score          
                    ),                    

                    round(                  
                        home_probability,  
                        1                  
                    )                       

                )                          

            )                               

            home_signal = True
     
        # AWAY WIN

        away_score = (

            (
                away_form["total_scored"]
                -
                home_form["total_scored"]
            ) * 0.5

           +
         
           (
                away_form["goal_diff"]
                -
                home_form["goal_diff"]
            ) * 0.5

            +

            (
                away_form["recent_goal_diff"]  
                -                               
                home_form["recent_goal_diff"]  
            ) * 0.7                            

            +

            (
                home_form["losses"]
                -
                away_form["losses"]
            ) * 2

            +

            (
                away_form["form_pct"]
                -
                home_form["form_pct"]
            ) * 0.4

            +

            (
                away_form["recent_form_pct"]    
                -                                
                home_form["recent_form_pct"]     
            ) * 0.3   

            +

            (
                away_form["momentum"]      
                -                          
                home_form["momentum"]      
            ) * 0.4                         

            +
         
            (
               away_form["unbeaten_pct"]
               -
               home_form["unbeaten_pct"]
            ) * 0.1

            +

            (
                away_form["avg_scored"]
                -
                home_form["avg_scored"]
            ) * 8

            +
         
           (
               away_form["recent_avg_scored"]       
               -                                    
               home_form["recent_avg_scored"]        
            ) * 6                                     

            +

            (
               home_form["avg_conceded"]
               -
               away_form["avg_conceded"]
            ) * 5

           +
           (
              home_form["recent_avg_conceded"]      
              -                                   
              away_form["recent_avg_conceded"]    
           ) * 4                                     

           )

        away_score *= 0.6

        print(                    

            "AWAY RAW SCORE:",     

            home,                  

            away,                  

            away_score             

        )      

      
        
        strength_gap = (               

            away_strength                

            -                           

            home_strength               

        )                                

        away_score += (                 

            strength_gap                 

            *                           

            0.35                        

        )                              

        if (                            

            strength_gap >= 25           

        ):                              

            away_score += 3              

        elif (                           

            strength_gap >= 15          

        ):                              

            away_score += 1     


        # ATTACK / DEFENSE INDEX      

        attack_gap = (               

            (                        

                away_form["avg_scored"]         

                +                              

                away_form["recent_avg_scored"]  

            )                                 

            -                                  

            (                                  

                home_form["avg_conceded"]       

                +   

                home_form["recent_avg_conceded"]

            )                                  

        )                                      

        if (                                   

            attack_gap >= 1.80                

        ):                                     

            away_score += 4                   

        elif (                                 

            attack_gap >= 1.40                

        ):                                     

            away_score += 2                   

        elif (                               

            attack_gap >= 1.00                 

        ):                                    

            away_score += 1                   
     

        print(                      

            "AWAY BASE SCORE:",      

            home,                    

            away,                   

            away_score              

        )                           

        away_score -= h2h * 2    


        # GOAL DIFFERENCE BONUS       

        goal_diff = (                

            away_form["avg_scored"]   

            -                         

            away_form["avg_conceded"]  

        )                              

        if (                          

            goal_diff >= 1.40         

        ):                             

            away_score += 4           

        elif (                        

            goal_diff >= 0.90          

        ):                             

            away_score += 2         

        elif (                         

            goal_diff >= 0.50          

        ):                             

            away_score += 1          


        # AWAY VENUE BONUS             

        if (                           

            away_form["away_games"] >= 3 

        ):                              

            away_away_winrate = (       

                away_form["away_wins"] 

                /                       

                away_form["away_games"] 

            ) * 100                     

            if (                        

                away_away_winrate >= 70 

            ):                          

                away_score += 5         

            elif (                     

                away_away_winrate >= 55 

            ):                          

                away_score += 3         
        
        if h2h <= -4:
             away_score += 4

        elif h2h <= -2:
             away_score += 2

        if away_form["away_games"] > 0:   

            away_score += (               

                (
                    away_form["away_wins"] 
                    /
                    away_form["away_games"] 
                )

                * 5                       

            )                            

        if match_odds:                    

            away_edge_score = odds_score( 
                min(95, away_score),     
                match_odds[2]            
            )        

            print(                     

                "AWAY EDGE SCORE:",      
        
                home,                     
        
                away,                     
        
                away_edge_score           
        
            )                            


            away_score += (             
                away_edge_score          
                *                        
                0.25                      
            )            

       

        print(
            "AFTER HOME DROP:",
            home_score
        )
        
        # FORM COLLAPSE BONUS

        if (
            home_form["losses"] >= 5
            or
            home_form["form_pct"] <= 35
        ):

            away_score += 6

          

        # GOAL DOMINANCE BONUS          

        if (                            

            away_form["avg_scored"]     
            >=                         
            2.0                         

            and                          

            home_form["avg_conceded"]    
            >=                           
            1.4                          

        ):                               

            away_score += 3      


        # EXPECTED GOALS BONUS         

        if (                           

            expected_goals >= 3.2       

            and                       

            away_form["avg_scored"]     
            >=                         
            1.8                         

            and                         

            home_form["avg_conceded"]  
            >=                          
            1.3                         

        ):                             

            away_score += 4            

        elif (                         

            expected_goals >= 2.8      

        ):                             

            away_score += 2            
     

               # SUPER FORM BONUS             

        if (                          

            away_form["form_pct"] >= 80 

            and                         

            away_form["wins"] >= 6      

        ):                              

            away_score += 3     

        print(
            "AFTER COLLAPSE:",
            home_score
        )

        # DEFENSIVE BONUS             

        if (                           

            away_form["clean_sheet_pct"] >= 50  

        ):                             

            away_score += 2            

        elif (                        

            away_form["clean_sheet_pct"] >= 35  

        ):                              

            away_score += 1            
         
 
        away_edge = (


         
            away_form["wins"]
            -
            home_form["wins"]
        )


        # WIN EDGE BONUS                 

        if (                            

            away_edge >= 4               

        ):                              

            away_score += 3             

        elif (                          

            away_edge >= 2               

        ):                               

            away_score += 1             

        away_gap = (
            away_form["form_pct"]
            -
            home_form["form_pct"]
        )

        # MOMENTUM BONUS                

        momentum_gap = (              

            away_form["momentum"]      

            -                         

            home_form["momentum"]      

        )                             

        if (                           

            momentum_gap >= 20         

        ):                            

            away_score += 3           

        elif (                        

            momentum_gap >= 10         

        ):                             

            away_score += 1           

        recent_away_gap = (                   
            away_form["recent_form_pct"]       
            -
            home_form["recent_form_pct"]    
        )      


        # RECENT FORM EXPLOSION BONUS      

        if (                              

            recent_away_gap >= 35        

        ):                                

            away_score += 5              

        elif (                           

            recent_away_gap >= 25         

        ):                               

            away_score += 3              

        elif (                           

            recent_away_gap >= 15         

        ):                                

            away_score += 1              

        # RECENT GOAL DIFF BONUS         

        if (                             

            away_form["recent_goal_diff"] >= 6  

        ):                              

            away_score += 3

        elif (                           

            away_form["recent_goal_diff"] >= 3  

        ):                              

            away_score += 1             
             
        away_super_value = False
        away_value = False

        if match_odds:                 

            if (                        
        
                match_odds[2] is not None
        
            ):                         
        
                edge = value_edge(                

                    min(95, away_score),         
                
                    match_odds[2]                 
                
                )                                          
        
                if edge >= 12:          
        
                    away_super_value = True   
        
                    away_score += 3           
        
                elif edge >= 6:       
        
                    away_value = True   
        
                    away_score += 1     
        
        away_odds_ok = True             
    
        if (                            

            match_odds                  

            and                         

            match_odds[2] is not None   

        ):      

            away_odds_ok = (
                1.40 <= match_odds[2] <= 3.20
            )
         
            print(  
                
                "AWAY SCORE:",        
                home,                 
                away,                
                away_score,           
                away_strength,       
                home_strength,        
                h2h                   
            )         

        

        # EXTREME MOMENTUM BONUS         

        if (                             

            home_form["momentum"] >= 25   

        ):                                

            home_score += 3              

        if (                             

            away_form["momentum"] >= 25   

        ):                               

            away_score += 3               

        home_score = min(                

            80,                         

            max(                         

                -80,                     

                home_score                

            )                            

        )                                

        away_score = min(               

            80,                          

            max(                         

                -80,                      

                away_score               

            )                             

        )                               

        total_strength = (               

            max(                          

                1,                        

                (

                    home_score           

                    +                    

                    80                   

                )                        

                +

                (

                    away_score           

                    +                    

                    80                   

                )                        

            )                             

        )                                 

        home_probability = round(        

            (

                max(                     

                    1,                   

                    home_score           

                    +                    

                    80                   

                )                         

                /                         

                total_strength           

            )                            

            *                            

            100,                          

            1                            

        )                                 

        away_probability = round(        

            (

                max(                     

                    1,                    

                    away_score            

                    +                    

                    80                    

                )                         

                /                        

                total_strength           

            )                            

            *                            

            100,                         

            1                             

        )                                

        score_gap = abs(                 

            home_score                    

            -                             

            away_score                   

        )                                 

        away_score_gap = (               

            away_score                    

            -                            

            home_score                   

        )                                 

                  

                              
        # DOMINANCE BONUS                

        if (                             

            score_gap >= 50               

        ):                               

            if (                         

                home_score > away_score  

            ):                            

                home_score += 2          

            else:                         

                away_score += 2          

        elif (                           

            score_gap >= 35               

        ):                               

            if (                         

                home_score > away_score   

            ):                            

                home_score += 1          

            else:                        

                away_score += 1      


        away_score = min(         
            80,                   
            max(                   
                -80,               
                away_score         
            )                      
        )   

        total_strength = max(                  
            1,                               
            home_score                       
            +                               
            away_score                       
            +                                
            200                             
        )                                    

        home_probability = round(             
            (                               
                max(                         
                    1,                        
                    home_score + 100          
                )                            
                /                            
                total_strength               
            )                               
            *                               
            100,                             
            1                               
        )                                    

        away_probability = round(            
            (                               
                max(                          
                    1,                       
                    away_score + 100          
                )                            
                /                            
                total_strength              
            )                                
            *                               
            100,                             
            1                                
        )     


        # QUALITY CONFIRMATION         

        if (                            

            away_form["wins"] >= 5      

            and                         

            away_form["losses"] <= 1    

            and                          

            away_form["form_pct"] >= 70  

            and                         

            away_edge >= 3               

            and                          

            market_away >= 52            

        ):                              

            away_score += 3              

        elif (                           

            away_form["wins"] >= 4      

            and                         

            away_form["form_pct"] >= 65  

            and                          

            away_edge >= 2              

        ):                              

            away_score += 1              

        # STRONG AWAY BONUS            

        if (                          

            away_probability >= 72      

            and                        

            away_edge >= 3              

            and                         

            away_form["wins"] >= 4     

            and                        

            away_form["losses"] <= 1    

        ):                              

            away_score += 4             

        elif (                        

            away_probability >= 68      

            and                         

            away_edge >= 2              

        ):                              

            away_score += 2             

        
        # CONSENSUS BONUS              

        if (                           

            away_probability >= 70     

            and                         

            away_edge >= 3             

            and                         

            market_away >= 55         

        ):                             

            away_score += 3            
     

        if (
            home_probability >= 65
            and
            home_score >= 40
        ):

            home_score += 1

        elif (
            home_probability >= 68
            and
            home_score >= 30
        ):

            home_score += 1

        if (                               
            away_probability >= 65          
            and                             
            away_score >= 40               
        ):                                  

            away_score += 1                

        elif (                              
            away_probability >= 68          
            and                             
            away_score >= 30               
        ):                                  

            away_score += 1               

        away_score_gap = (
            away_score
            -
            home_score
        )


        away_balance = (                  

            away_form["avg_scored"]        

            -                             

            away_form["avg_conceded"]      

        )                                 

        if (                              

            away_balance < 0.30            

        ):                                

            return None    

        # SUPER DOMINANCE FILTER     

        goal_gap = (                  

            away_form["avg_scored"]   

            -                        

            home_form["avg_scored"]  

        )                            

        defense_gap = (             

            home_form["avg_conceded"] 

            -                        

            away_form["avg_conceded"] 

        )                            

        dominance_ok = (             

            goal_gap >= 0.40         

            and                       

            defense_gap >= 0.30       

        )       


        # FORM CONSISTENCY          

        consistency_ok = (            

            away_form["wins"]         

            >=                        

            away_form["losses"] * 2   

        )                             
     
        
        if (
            away_score >= 50          
            and
            away_odds_ok
            and
            away_form["unbeaten_pct"] >= 60
            and
            away_form["wins"] >= 2
            and                          
            away_form["losses"] <= 3    
            and                         
            away_form["draws"] <= 4     
            and                        
            away_edge >= 2                  
            and
            away_score_gap >= 15
            and
            away_gap >= 15
            and
            recent_away_gap >= 5
            and
            away_form["avg_scored"] >= 1.5
            and
            (
                away_form["avg_scored"]
                -
                home_form["avg_scored"]
            ) >= 0.30
            and                          

            (
                away_form["avg_scored"]   

                -

                away_form["avg_conceded"]

            ) >= 0.30                     
            and                                   
            away_form["recent_avg_scored"] >= 1.5 
            and                          
            away_form["recent_goal_diff"] >= 1  
            and
            away_form["avg_conceded"] <= 1.3
            and
            home_form["avg_conceded"] >= 1.2
            and                                   
            home_form["recent_avg_conceded"] >= 1.2 
            and
            away_probability >= 65        
            and
            dominance_ok
            and
            consistency_ok
        ):                               

            print(                       
                "AWAY SIGNAL:",          
                away_score                
            )                            

            signals.append(              

                (                         

                    "✈️ AWAY WIN",        

                    confidence_from_score(
                        away_score       
                    ),                   

                    round(               
                        away_probability, 
                        1                 
                    )                     

                )                        

            )                            

        print(                           
            "OVER CHECK:",               
            home,
            away,
            over_prob,
            home_form["over25"],
            away_form["over25"]
        )
        # OVER 2.5

        over_league = league_score(
            country,
            "⚽ OVER 2.5"
        )

        over_value = expected_goals * 10     

        over_final = calculate_final_score(

            form_score,
            over_prob,

            over_value,
            over_league

        )

        over_conf = confidence_from_score(
            over_final
        )

        print(                                 
             "OVER SCORE:",                  
             home,                            
             away,                             
             over_prob,                       
             over_final,                      
             over_conf                         
        )                                    
        print(                          
            "OVER DETAILS:",            
            home,                       
            away,                       
            over_prob,                  
            over_conf,                   
            home_form["avg_scored"],    
            away_form["avg_scored"],    
            home_form["avg_conceded"],   
            away_form["avg_conceded"]    
        )      

        over_signal = False

        print(
            "OVER FILTERS:",
            "xG=", expected_goals,
            "Havg=", home_form["avg_scored"],
            "Aavg=", away_form["avg_scored"],
            "Hravg=", home_form["recent_avg_scored"],
            "Aravg=", away_form["recent_avg_scored"],
            "Hcon=", home_form["avg_conceded"],
            "Acon=", away_form["avg_conceded"],
            "Hov=", home_form["over25_pct"],
            "Aov=", away_form["over25_pct"],
            "Hrecent=", home_form["recent_over25"],
            "Arecent=", away_form["recent_over25"],
            "Hclean=", home_form["clean_sheet_pct"],
            "Aclean=", away_form["clean_sheet_pct"]
        )
     
        if (
            over_prob >= 70
            and
            over_conf >= 70
            and                           
            expected_goals >= 3.5         
            and
            (
                home_form["avg_scored"]
                +
                away_form["avg_scored"]
            ) >= 3.0
            and                              
            (
                home_form["recent_avg_scored"]
                +
                away_form["recent_avg_scored"]
            ) >= 3.0
            and
            home_form["avg_conceded"] >= 1.0
            and                                            
            away_form["avg_conceded"] >= 1.0 
            and                           
            home_form["clean_sheet_pct"] <= 50  
            and                          
            away_form["clean_sheet_pct"] <= 50   
            and                                  
            home_form["recent_avg_conceded"] >= 1.0 
            and                                    
            away_form["recent_avg_conceded"] >= 1.0 
            and                         
            home_form["over25_pct"] >= 58 
            and                         
            away_form["over25_pct"] >= 58 
            and                         
            (                           
                home_form["over25"]      
                +                       
                away_form["over25"]   
            ) >= 4     

         and
         (
               home_form["recent_over25"]
               +
               away_form["recent_over25"]
        ) >= 5
        ):

            signals.append(

                (
                    "⚽ OVER 2.5",
                    over_conf,
                    round(
                        over_prob,
                        1
                    )
                )

            )

            over_signal = True
         
        print(
            "BTTS CHECK:",
            home,
            away,
            btts_prob,
            home_form["btts"],
            away_form["btts"]
        )

        # BTTS

        btts_league = league_score(
            country,
            "💎 BTTS"
        )

        btts_value = (                    
            home_form["recent_avg_scored"] 
            +                              
            away_form["recent_avg_scored"]        
        ) * 5                            

        btts_final = calculate_final_score(

            form_score,
            btts_prob,

            btts_value,
            btts_league

        )

        btts_conf = confidence_from_score(
            btts_final
        )

        print(                             
            "BTTS SCORE:",                 
            home,                           
            away,                           
            btts_prob,                     
            btts_final,                      
            btts_conf                     
        )                                    

        if (
            btts_prob >= 73
            and
            btts_conf >= 73
            and                          
            expected_goals >= 3.2         
            and
            home_form["avg_scored"] >= 1.3
            and
            away_form["avg_scored"] >= 1.3
            and                              
            home_form["recent_avg_scored"] >= 1.3  
            and                               
            away_form["recent_avg_scored"] >= 1.3  
            and
            home_form["scored_pct"] >= 75
            and
            away_form["scored_pct"] >= 75                      
            and
            home_form["recent_goal_diff"] > -3
            and
            away_form["recent_goal_diff"] > -3
            and        
            min(
                home_form["recent_avg_scored"],
                away_form["recent_avg_scored"]
            ) >= 1.3
            and
            (
                home_form["btts"]          
                /
                home_form["played"]          
            ) >= 0.60                        

            and                             

            (
                away_form["btts"]            
                /
                away_form["played"]         
            ) >= 0.60                               
            and                                   
            home_form["recent_avg_conceded"] >= 0.8
            and                                   
            away_form["recent_avg_conceded"] >= 0.8
            and
            home_form["clean_sheet_pct"] <= 40
            and
            away_form["clean_sheet_pct"] <= 40
        ):

            signals.append(

                (
                    "💎 BTTS",
                    btts_conf,
                    round(
                        btts_prob,
                        1
                    )
                )

            )

            print("CHECKING:", home, "vs", away)

        # HOME OVER 1.5                  

        if (                              

            home_score >= 45               
            and                            
            home_probability >= 72         
            and                            
            expected_goals >= 3.3          
            and                           
            home_form["avg_scored"] >= 1.8 
            and                            
            home_form["recent_avg_scored"] >= 1.8 
            and                           
            away_form["avg_conceded"] >= 1.3 
            and                           
            away_form["recent_avg_conceded"] >= 1.2 
            and                           
            home_form["scored_pct"] >= 85  
            and                            
            away_form["clean_sheet_pct"] <= 40 
            and                           
            home_form["recent_goal_diff"] >= 3 
            and                            
            home_form["form_pct"] >= 60    
            and                            
            home_strength > away_strength  
            and                            
            home_edge >= 2                
            and                            
            match_odds                   
            and                            
            match_odds[0] is not None      
            and                            
            1.35 <= match_odds[0] <= 2.80  

        ):                                

            signals.append(                

                (                          

                    "🏠 HOME OVER 1.5",   

                    confidence_from_score( 
                        home_score        
                    ),                     

                    round(                
                        home_probability,  
                        1                  
                    )                      

                )                         

            )       

            home_over15_signal = True

        # AWAY OVER 1.5                   

        if (                                        

            away_score >= 45                        
            and                                     
            away_probability >= 72                 
            and                                     
            expected_goals >= 3.3                   
            and                                    
            away_form["avg_scored"] >= 1.8          
            and                                     
            away_form["recent_avg_scored"] >= 1.8   
            and                                     
            home_form["avg_conceded"] >= 1.3        
            and                                     
            home_form["recent_avg_conceded"] >= 1.2 
            and                                     
            away_form["scored_pct"] >= 85          
            and                                     
            home_form["clean_sheet_pct"] <= 40      
            and                                     
            away_form["recent_goal_diff"] >= 3     
            and                                    
            away_form["form_pct"] >= 60             
            and                                     
            away_strength > home_strength           
            and                                     
            away_edge >= 2                          

        ):                                         

            signals.append(                         

                (                                 

                    "✈️ AWAY OVER 1.5",             

                    confidence_from_score(          
                        away_score                  
                    ),                             

                    round(                         
                        away_probability,          
                        1                           
                    )                              

                )                                  

            )      

            away_over15_signal = True


        # UNDER 2.5                  

        under_prob = (                

            100                       

            -                         

            over_prob                  

        )                             

        if (                          

            over_prob <= 50            

            and                        

            expected_goals <= 2.0      

            and                        

            home_form["avg_scored"] <= 1.2      

            and                       

            away_form["avg_scored"] <= 1.2      

            and       

            home_form["recent_avg_scored"] <= 1.3
            
            and
            
            away_form["recent_avg_scored"] <= 1.3

            and

            home_form["avg_conceded"] <= 1.2    

            and                        

            away_form["avg_conceded"] <= 1.2    

            and                          

            home_form["clean_sheet_pct"] >= 35  

            and                           

            away_form["clean_sheet_pct"] >= 35   

            and                       

            home_form["over25_pct"] <= 45      

            and                       

            away_form["over25_pct"] <= 45       

        ):                            

            signals.append(            

                (                      

                    "🛡 UNDER 2.5",    

                    confidence_from_score(70), 

                    round(             

                        under_prob,    

                        1              

                    )                  

                )                      

            )       

   

        # OVER 3.5                     

        if (                          

            over_prob >= 78            

            and                        

            expected_goals >= 4.5   

            and                        

            home_form["avg_scored"] >= 1.8      

            and                        

            away_form["avg_scored"] >= 1.6     

            and                        

            home_form["over25_pct"] >= 73       

            and                        

            away_form["over25_pct"] >= 73      

            and                        

            (                          

                home_form["recent_over25"]      

                +                     

                away_form["recent_over25"]      

            ) >= 6                     

        ):                             

            signals.append(            

                (                      

                    "🚀 OVER 3.5",     

                    over_conf,        

                    round(            

                        over_prob,     

                        1             

                    )                  

                )                      

            )                          
            
         
        signals.sort(                 

            reverse=True,             

            key=lambda x: (           

                x[1]                 

                +                    

                x[2]                  

            ) / 2                     

        )             

        print(
            "SIGNAL:",
            signals
        )

        signals = signals[:2]        
         
        print(
            "RETURN SIGNALS:", 
            home, 
            away, 
            len(signals)
        )
     
        return signals

    except Exception as e:
                           
        print(
            "PREMATCH ERROR:",
            repr(e)
        )

        return None
        
# =========================================================
# SEND PREMATCH SIGNAL
# =========================================================

def send_prematch_signal(        

    fixture_id,                  

    match_date,                 
    kickoff_time,                

    country,                     
    league,                      

    home,                        
    away,                        

    market,                       

    confidence,                  
    probability,                  
    odds_text                     

):                                

    message = f"""               
🔥 PREMATCH V3

🏆 {home} vs {away}

🗓 Date: {match_date}
🕒 Kickoff: {kickoff_time}

🌍 {country}
🏟 {league}

🔥📊 Market:
{market}🔥

🎯 Probability:
{probability}%

💰 Odds:
{odds_text}

💎 Confidence:
{confidence}%
"""                               

    send_telegram(message)        

    save_signal(                  

        fixture_id,               

        country,                  
        league,                   

        home,                     
        away,                    

        market,                   

        0,                       
        confidence               

    )                             

    print(                       
        "SIGNAL SAVED:",          
        fixture_id,               
        market,                   
        confidence                
    )                             

# =========================================================
# PREMATCH LOOP
# =========================================================

def prematch_loop():

    print("PREMATCH SCAN START")

    matches = get_upcoming_matches()

    print(
        f"Matches found: {len(matches)}"
    )

    all_signals = []

    for match in matches:

        signals = analyze_prematch_match(
            match
        )

        if not signals:
            continue

        fixture_id = match["fixture"]["id"]

        match_odds = get_match_odds(
            fixture_id
        )

        print(
            "MATCH ODDS:",
            fixture_id,
            match_odds
        )

        if not match_odds:
            continue

       

        country = match["league"]["country"]
        league = match["league"]["name"]

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        fixture_time = datetime.fromisoformat(
            match["fixture"]["date"].replace(
                "Z",
                "+00:00"
            )
        ).astimezone(TZ)

        match_date = fixture_time.strftime(
            "%d.%m.%Y"
        )

        kickoff_time = fixture_time.strftime(
            "%H:%M"
        )      
   
     
        for market, confidence, probability in signals:
         
            print(                 
                "DEBUG SIGNAL:",   
                market,           
                confidence,       
                probability        
            )                    

        odds_text = "-"                  

        if match_odds:                  

            if (                         

                "HOME WIN" in market     
                and                      
                match_odds[0] is not None

            ):                           

                odds_text = str(        

                    match_odds[0]       

                )                        

            elif (                       

                "AWAY WIN" in market     
                and                      
                match_odds[2] is not None

            ):                           

                odds_text = str(        

                    match_odds[2]        

                )                       

        all_signals.append(            

            (                          

                probability,           
                fixture_id,            

                match_date,            
                kickoff_time,           

                country,               
                league,                 

                home,                  
                away,                   

                market,                
                confidence,            
                odds_text               

            )                           

        )                               

    all_signals.sort(
        reverse=True,
        key=lambda x: x[0]
    )

    top_signals = all_signals[:3] 

    for (
        probability,
        fixture_id,

        match_date,
        kickoff_time,

        country,
        league,

        home,
        away,

        market,
        confidence,
        odds_text,
       
    ) in top_signals:

        key = f"{fixture_id}_{market}"

        if key in sent_prematch:

            if (
                time.time()
                -
                sent_prematch[key]
            ) < 86400:

                continue

        sent_prematch[key] = time.time()

        print(
            market,
            confidence,
            probability
        )      

             

        send_prematch_signal(

            fixture_id,

            match_date,
            kickoff_time,

            country,
            league,

            home,
            away,

            market,

            confidence,
            probability,
            odds_text,
           
        )




# =========================================================
# LIVE LOOP
# =========================================================

def live_loop():

    matches = get_live_matches()

    print(f"Live matches: {len(matches)}")

    print("LIVE SCAN START")

    for match in matches:

        signal = analyze_live_match(
            match
        )

        if not signal:
            continue

        fixture_id = match["fixture"]["id"]

        home_goals = match["goals"]["home"] or 0
        away_goals = match["goals"]["away"] or 0

        key = (
            f"live_{fixture_id}_"
            f"{home_goals}_{away_goals}"
        )

        if key in sent_live:
            continue

        sent_live[key] = time.time()

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        home_goals = (
            match["goals"]["home"] or 0
        )

        away_goals = (
            match["goals"]["away"] or 0
        )

        minute = signal[2]

        goal_probability = signal[3]
        
        stats = get_statistics(
            fixture_id
        )

        home_pressure = 0
        away_pressure = 0

        home_shots = 0
        away_shots = 0 

        home_corners = 0
        away_corners = 0
        
        home_xg = 0            
        away_xg = 0            

        if len(stats) >= 2:

            home_pressure = calculate_pressure(
                stats[0]
            )

            away_pressure = calculate_pressure(
                stats[1]
            )


            home_form = get_team_form(     
                match["teams"]["home"]["id"],
                venue="home"              
            )                            

            away_form = get_team_form(     
                match["teams"]["away"]["id"],
                venue="away"              
            )                             

            home_shots = extract(
                stats[0],
                "Shots on Goal"
            )

            away_shots = extract(
                stats[1],
                "Shots on Goal"
            )

            home_corners = extract(
                stats[0],
                "Corner Kicks"
            )

            away_corners = extract(
                stats[1],
                "Corner Kicks"
            )           
        
        country = match["league"]["country"]      
        league = match["league"]["name"]         

        odds_text = "-"                          

        match_odds = get_match_odds(              

            fixture_id                           

        )                                         

        if match_odds:                           

            if "HOME" in signal[0]:              

                odds_text = str(                  

                    match_odds[0]                 

                )                               

            elif "AWAY" in signal[0]:             

                odds_text = str(                  

                    match_odds[2]                 

                )                               

        send_telegram(                            

            f"""                                
🔥 LIVE SIGNAL

🏆 {home} vs {away}

🌍 {country}
🏟 {league}

📊 Score:
{match["goals"]["home"] or 0} - {match["goals"]["away"] or 0}

⏱ Minute: {minute}

🔥{signal[0]}🔥

💰 Odds:
{odds_text}

💎 Confidence: {signal[1]}%

🎯 Goal Probability:
{goal_probability}%
"""
)          
         
if __name__ == "__main__":

    print("MAIN V3 STARTED")
   
    init_database()

    while True:

        prematch_loop()

        live_loop()

        time.sleep(300)




























































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































