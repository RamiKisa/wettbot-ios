import json, os, datetime, urllib.request, urllib.parse, pathlib
KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
OUT=pathlib.Path("recommendations.json")
now=datetime.datetime.now(datetime.timezone.utc)
leagues={"Premier League":39,"Bundesliga":78,"La Liga":140,"Serie A":135,"Ligue 1":61,"Primeira Liga":94,"Champions League":2,"Europa League":3,"Conference League":848}
def save(x): OUT.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding="utf-8")
def api(path,params):
    req=urllib.request.Request("https://v3.football.api-sports.io/"+path+"?"+urllib.parse.urlencode(params),headers={"x-apisports-key":KEY,"Accept":"application/json","User-Agent":"WettbotFree/2.0"})
    with urllib.request.urlopen(req,timeout=25) as resp: data=json.load(resp)
    if data.get("errors"): raise RuntimeError(str(data["errors"]))
    return data.get("response",[])
if not KEY:
    save({"generatedAt":now.isoformat(),"status":"needs_api_key","message":"Für automatische Spielanalysen wird ein kostenloser API-Football-Schlüssel als GitHub Secret API_FOOTBALL_KEY benötigt.","recommendations":[],"fixtures":[]})
    print("API key missing; wrote setup status."); raise SystemExit(0)
start=now.date().isoformat(); end=(now.date()+datetime.timedelta(days=7)).isoformat()
fixtures=[]; errors=[]
for name,lid in leagues.items():
    try:
        rows=api("fixtures",{"league":lid,"season":now.year if now.month>=7 else now.year-1,"from":start,"to":end,"timezone":"Europe/Berlin"})
        for row in rows:
            f=row.get("fixture",{}); teams=row.get("teams",{}); lg=row.get("league",{})
            if not f.get("date"): continue
            dt=datetime.datetime.fromisoformat(f["date"].replace("Z","+00:00"))
            if dt<=now: continue
            fixtures.append({"id":f.get("id"),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":name,"home":teams.get("home",{}).get("name"),"away":teams.get("away",{}).get("name"),"venue":(f.get("venue") or {}).get("name"),"status":"scheduled"})
    except Exception as e: errors.append(name+": "+str(e)[:150])
fixtures.sort(key=lambda f:f["timestamp"])
# 45 fixture cap plus one prediction request per fixture keeps daily quota manageable.
fixtures=fixtures[:45]
analyses=[]
for f in fixtures:
    try:
        rows=api("predictions",{"fixture":f["id"]})
        if not rows: f["analysisStatus"]="prediction_unavailable"; continue
        row=rows[0]; p=row.get("predictions",{}) or {}; comp=row.get("comparison",{}) or {}
        pct=p.get("percent",{}) or {}; goals=p.get("goals",{}) or {}
        h2h=row.get("h2h",[]) or []
        # Probability percentages are provider estimates, not calibrated guarantees.
        def pctnum(v):
            try:return round(float(str(v).replace("%","").strip()),1)
            except:return None
        f.update({
          "prediction":{"winner":(p.get("winner") or {}).get("name"),"winnerComment":(p.get("winner") or {}).get("comment"),"advice":p.get("advice"),"underOver":p.get("under_over"),"homeGoals":goals.get("home"),"awayGoals":goals.get("away"),"confidence":p.get("confidence"),"probabilities":{"home":pctnum(pct.get("home")),"draw":pctnum(pct.get("draw")),"away":pctnum(pct.get("away"))},"comparison":comp},
          "h2h":h2h[:5],"analysisStatus":"available"})
        analyses.append(f)
    except Exception as e:
        f["analysisStatus"]="error"; errors.append("Spiel "+str(f["id"])+": "+str(e)[:120])
save({"generatedAt":now.isoformat(),"status":"ok","message":"Prognosen ohne Quoten. Wahrscheinlichkeiten stammen aus dem externen Modell und sind nicht garantiert. Fehlende Daten werden markiert, nicht erfunden.","recommendations":analyses,"fixtures":fixtures,"errors":errors[:12],"source":"API-Football"})
print("fixtures=%s analyses=%s errors=%s"%(len(fixtures),len(analyses),len(errors)))
