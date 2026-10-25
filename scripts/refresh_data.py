import json, os, datetime, urllib.request, urllib.parse, pathlib, math, re
from collections import defaultdict

KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
OUT=pathlib.Path("recommendations.json")
now=datetime.datetime.now(datetime.timezone.utc)
leagues={"Premier League":39,"Bundesliga":78,"La Liga":140,"Serie A":135,"Ligue 1":61,"Primeira Liga":94,"Champions League":2,"Europa League":3,"Conference League":848}

def save(payload):
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")

def api(path, params):
    q=urllib.parse.urlencode(params)
    req=urllib.request.Request("https://v3.football.api-sports.io/"+path+"?"+q,headers={"x-apisports-key":KEY,"Accept":"application/json","User-Agent":"WettbotFree/1.0"})
    with urllib.request.urlopen(req,timeout=25) as resp: data=json.load(resp)
    if data.get("errors"): raise RuntimeError(str(data["errors"]))
    return data.get("response",[])

if not KEY:
    save({"generatedAt":now.isoformat(),"status":"needs_api_key","message":"Für automatische Prognosen und Quoten fehlt der GitHub-Secret API_FOOTBALL_KEY. API-Schlüssel hinzufügen und Workflow erneut starten.","recommendations":[],"fixtures":[]})
    print("API_FOOTBALL_KEY missing; wrote setup status.")
    raise SystemExit(0)

start=now.date().isoformat()
end=(now.date()+datetime.timedelta(days=7)).isoformat()
fixtures=[]
errors=[]
# Weekend leagues and European cups according to kickoff date; pull all configured leagues and filter upcoming fixtures.
for name,lid in leagues.items():
    try:
        rows=api("fixtures",{"league":lid,"season":now.year if now.month>=7 else now.year-1,"from":start,"to":end})
        for row in rows:
            f=row.get("fixture",{}); teams=row.get("teams",{}); league=row.get("league",{})
            dt=f.get("date")
            if not dt: continue
            kickoff=datetime.datetime.fromisoformat(dt.replace("Z","+00:00"))
            if kickoff<=now: continue
            fixtures.append({"id":f.get("id"),"date":kickoff.isoformat(),"timestamp":kickoff.timestamp(),"league":name,"home":teams.get("home",{}).get("name"),"away":teams.get("away",{}).get("name"),"venue":(f.get("venue") or {}).get("name")})
    except Exception as e: errors.append(name+": "+str(e)[:180])

# Keep a broad candidate pool to stay within free request quota.
fixtures.sort(key=lambda f:f["timestamp"])
fixtures=fixtures[:45]
recommendations=[]
for f in fixtures:
    try:
        pred=api("predictions",{"fixture":f["id"]})
        if not pred: continue
        p=pred[0].get("predictions",{}) or {}
        pct=p.get("percent",{}) or {}
        winner=p.get("winner",{}) or {}
        # Convert provider prediction percentages into structured, explicit market candidates.
        candidates=[]
        for key,sel in (("home","1"),("draw","X"),("away","2")):
            try: prob=float(str(pct.get(key,"0")).replace("%","").strip())/100
            except: prob=0
            if prob>0 and prob<1: candidates.append({"market":"1X2","selection":sel,"probability":prob,"source":"API-Football prediction"})
        advice=p.get("advice")
        # Avoid inventing odds. Odds must be sourced from the bookmaker feed below.
        oddsrows=api("odds",{"fixture":f["id"]})
        for orow in oddsrows:
            for bm in orow.get("bookmakers",[]) or []:
                bmname=str(bm.get("name",""))
                if not re.search(r"bet365|betano",bmname,re.I): continue
                for bet in bm.get("bets",[]) or []:
                    bname=str(bet.get("name",""))
                    for val in bet.get("values",[]) or []:
                        oddraw=val.get("odd")
                        try: odd=float(oddraw)
                        except: continue
                        v=str(val.get("value",""))
                        # Map only common, well-defined markets with matching probability.
                        prob=None; market=None; selection=None
                        if re.search(r"match winner|1x2|full.?time result",bname,re.I):
                            market="1X2"
                            selection={"Home": "1","Draw":"X","Away":"2"}.get(v)
                            prob=next((x["probability"] for x in candidates if x["selection"]==selection),None)
                        if prob is None or not selection or odd<1.5: continue
                        ev=prob*odd-1
                        recommendations.append({**f,"bookmaker":bmname,"market":market,"selection":selection,"odds":odd,"probability":round(prob,4),"ev":round(ev,4),"advice":advice,"winner":winner.get("name"),"confidence":p.get("confidence"),"reason":"API-Football percentage prediction matched to returned bookmaker price"})
    except Exception as e:
        errors.append("fixture "+str(f.get("id"))+": "+str(e)[:160])

# Deduplicate same match/market/selection, require model probability and minimum EV, rank probability then EV.
best={}
for r in recommendations:
    if r["probability"]<0.55 or r["ev"]<0.05: continue
    k=(r["id"],r["market"],r["selection"])
    if k not in best or r["odds"]>best[k]["odds"]: best[k]=r
ranked=sorted(best.values(),key=lambda r:(r["probability"],r["ev"]),reverse=True)[:10]
save({"generatedAt":now.isoformat(),"status":"ok","message":"Automatisch erzeugte Kandidaten; Prognosen sind keine Garantie. Quoten nur wenn API-Football sie für bet365/Betano tatsächlich geliefert hat.","recommendations":ranked,"fixtures":fixtures,"errors":errors[:12],"candidateCount":len(best),"source":"API-Football"})
print(f"fixtures={len(fixtures)} candidates={len(best)} top10={len(ranked)} errors={len(errors)}")
