import json, os, datetime, urllib.request, urllib.parse, pathlib
KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
OUT=pathlib.Path("recommendations.json"); now=datetime.datetime.now(datetime.timezone.utc)
season=now.year if now.month>=7 else now.year-1
leagues={"Premier League":39,"Bundesliga":78,"La Liga":140,"Serie A":135,"Ligue 1":61,"Primeira Liga":94,"Champions League":2,"Europa League":3,"Conference League":848}
BUDGET=92; used=0
def save(x): OUT.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding="utf-8")
def api(path,params):
 global used
 if used>=BUDGET: raise RuntimeError("Tagesbudget erreicht")
 req=urllib.request.Request("https://v3.football.api-sports.io/"+path+"?"+urllib.parse.urlencode(params),headers={"x-apisports-key":KEY,"Accept":"application/json","User-Agent":"WettbotFree/3.0"})
 with urllib.request.urlopen(req,timeout=25) as resp: d=json.load(resp)
 used+=1
 if d.get("errors"): raise RuntimeError(str(d["errors"]))
 return d.get("response",[])
def pct(v):
 try:return round(float(str(v).replace("%","").strip()),1)
 except:return None
if not KEY:
 save({"generatedAt":now.isoformat(),"status":"needs_api_key","message":"Für automatisierte Prognosen und Detaildaten wird ein API-Football-Schlüssel als GitHub Secret API_FOOTBALL_KEY benötigt.","recommendations":[],"fixtures":[]})
 print("API key missing"); raise SystemExit(0)
fixtures=[]; errors=[]; start=now.date().isoformat(); end=(now.date()+datetime.timedelta(days=7)).isoformat()
for name,lid in leagues.items():
 try:
  for row in api("fixtures",{"league":lid,"season":season,"from":start,"to":end,"timezone":"Europe/Berlin"}):
   f=row.get("fixture",{}); t=row.get("teams",{})
   if not f.get("date"): continue
   dt=datetime.datetime.fromisoformat(f["date"].replace("Z","+00:00"))
   if dt<=now: continue
   home=t.get("home") or {}; away=t.get("away") or {}
   fixtures.append({"id":f.get("id"),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":name,"home":home.get("name"),"away":away.get("name"),"homeId":home.get("id"),"awayId":away.get("id"),"venue":(f.get("venue") or {}).get("name"),"status":"scheduled","dataCoverage":{}})
 except Exception as e: errors.append(name+" fixtures: "+str(e)[:100])
fixtures.sort(key=lambda x:x["timestamp"]); fixtures=fixtures[:45]
for f in fixtures:
 try:
  rows=api("predictions",{"fixture":f["id"]})
  if rows:
   r=rows[0]; p=r.get("predictions") or {}; pctd=p.get("percent") or {}; goals=p.get("goals") or {}
   f["prediction"]={"winner":(p.get("winner") or {}).get("name"),"winnerComment":(p.get("winner") or {}).get("comment"),"advice":p.get("advice"),"underOver":p.get("under_over"),"homeGoals":goals.get("home"),"awayGoals":goals.get("away"),"confidence":p.get("confidence"),"probabilities":{"home":pct(pctd.get("home")),"draw":pct(pctd.get("draw")),"away":pct(pctd.get("away"))},"comparison":r.get("comparison") or {}}
   f["h2h"]=(r.get("h2h") or [])[:10]; f["analysisStatus"]="available"; f["dataCoverage"]["prediction"]="API-Football"
  else: f["analysisStatus"]="prediction_unavailable"; f["dataCoverage"]["prediction"]="Keine Prognose geliefert"
 except Exception as e: f["analysisStatus"]="prediction_error"; errors.append("prediction "+str(f["id"])+": "+str(e)[:80])
# Recent form: one API request per team, cached and reused across fixtures.
cache={}; team_ids=[]
for f in fixtures:
 for k in ("homeId","awayId"):
  if f.get(k) and f[k] not in team_ids: team_ids.append(f[k])
for tid in team_ids[:20]:
 if used>=BUDGET-15: break
 try:
  rows=api("fixtures",{"team":tid,"last":5,"season":season,"timezone":"Europe/Berlin"}); form=[]
  for r in rows:
   fx=r.get("fixture") or {}; t=r.get("teams") or {}; g=r.get("goals") or {}; hm=t.get("home") or {}; aw=t.get("away") or {}
   form.append({"date":fx.get("date"),"home":hm.get("name"),"away":aw.get("name"),"homeId":hm.get("id"),"awayId":aw.get("id"),"homeGoals":g.get("home"),"awayGoals":g.get("away"),"status":(fx.get("status") or {}).get("short")})
  cache[str(tid)]=form
 except Exception as e: errors.append("form "+str(tid)+": "+str(e)[:70])
for f in fixtures:
 f["form"]={"home":cache.get(str(f.get("homeId"))),"away":cache.get(str(f.get("awayId")))}
 f["dataCoverage"]["recentForm"]="API-Football" if f["form"]["home"] or f["form"]["away"] else "Keine Daten"
# Fixture-specific injury feed; lineups queried only for kickoffs within 48 hours.
for f in fixtures:
 if used>=BUDGET-10: break
 try:
  rows=api("injuries",{"fixture":f["id"]})
  f["injuries"]=[{"player":(x.get("player") or {}).get("name"),"type":(x.get("player") or {}).get("type"),"reason":(x.get("player") or {}).get("reason"),"team":(x.get("team") or {}).get("name")} for x in rows]
  f["dataCoverage"]["injuries"]="API-Football" if rows else "Quelle lieferte keine Einträge"
 except Exception as e: f["injuries"]=None; f["dataCoverage"]["injuries"]="Nicht abrufbar"
for f in fixtures:
 if used>=BUDGET: break
 if f["timestamp"]-now.timestamp()>48*3600: continue
 try:
  rows=api("fixtures/lineups",{"fixture":f["id"]})
  f["lineups"]=[{"team":x.get("team",{}).get("name"),"formation":x.get("formation"),"startXI":[(z.get("player") or {}).get("name") for z in x.get("startXI",[])],"substitutes":[(z.get("player") or {}).get("name") for z in x.get("substitutes",[])]} for x in rows]
  f["dataCoverage"]["lineups"]="API-Football" if rows else "Noch nicht veröffentlicht"
 except Exception: f["lineups"]=None; f["dataCoverage"]["lineups"]="Nicht abrufbar"
for f in fixtures:
 f["marketCoverage"]={"1X2":bool(f.get("prediction",{}).get("probabilities")),"overUnder":bool((f.get("prediction") or {}).get("underOver")),"btts":"Keine eigenständige Prognose integriert","corners":"Keine eigenständige Prognose integriert","cards":"Keine eigenständige Prognose integriert","playerProps":"Keine eigenständige Prognose integriert"}
save({"generatedAt":now.isoformat(),"status":"ok","message":"Datenabdeckung wird pro Spiel ausgewiesen. Nicht gelieferte Daten werden nicht erfunden.","recommendations":fixtures,"fixtures":fixtures,"errors":errors[:25],"source":"API-Football","requestsUsed":used,"dailyRequestBudget":BUDGET,"season":season})
print("fixtures=%s predictions=%s requests=%s errors=%s"%(len(fixtures),sum(bool(f.get("prediction")) for f in fixtures),used,len(errors)))
