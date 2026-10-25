import json, os, datetime, urllib.request, urllib.parse, pathlib
KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
OUT=pathlib.Path("recommendations.json"); now=datetime.datetime.now(datetime.timezone.utc)
season=now.year if now.month>=6 else now.year-1
leagues={"Premier League":("en.1.json","Europe/London"),"Bundesliga":("de.1.json","Europe/Berlin"),"La Liga":("es.1.json","Europe/Madrid"),"Serie A":("it.1.json","Europe/Rome"),"Ligue 1":("fr.1.json","Europe/Paris"),"Primeira Liga":("pt.1.json","Europe/Lisbon")}
uefa={"Champions League":2,"Europa League":3,"Conference League":848}
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
def fetch_team_news(team):
 if not team: return []
 q='"'+team+'" (Verletzung OR verletzt OR Ausfall OR gesperrt OR Aufstellung OR Pressekonferenz)'
 url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':q,'hl':'de','gl':'DE','ceid':'DE:de'})
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Wettbot/3.1 (RSS reader)','Accept':'application/rss+xml, application/xml'})
  with urllib.request.urlopen(req,timeout=12) as resp: raw=resp.read()
  import xml.etree.ElementTree as ET
  root=ET.fromstring(raw); items=[]
  for item in root.findall('.//item')[:5]:
   title=(item.findtext('title') or '').strip()
   link=(item.findtext('link') or '').strip()
   pub=(item.findtext('pubDate') or '').strip()
   source=(item.findtext('source') or '').strip()
   if title and link.startswith('https://'):
    items.append({'title':title,'url':link,'publishedAt':pub,'source':source or 'Google News RSS'})
  return items
 except Exception as e:
  return []
def enrich_team_news(fixtures, max_teams=24):
 teams=[]
 for f in fixtures:
  for name in (f.get('home'),f.get('away')):
   if name and name not in teams: teams.append(name)
 cache={}
 for team in teams[:max_teams]:
  cache[team]=fetch_team_news(team)
 for f in fixtures:
  f['teamNews']={'home':cache.get(f.get('home'),[]),'away':cache.get(f.get('away'),[])}
  f.setdefault('dataCoverage',{})['teamNews']='Google News RSS (Suchtreffer, keine bestätigten Ausfälle)' if (f['teamNews']['home'] or f['teamNews']['away']) else 'Keine RSS-Treffer / Abruf nicht verfügbar'
def fixture_fallback():
 all_matches=[]; errors=[]; history={}
 for league,(file,zone) in leagues.items():
  url="https://raw.githubusercontent.com/openfootball/football.json/master/%s/%s"%(str(season)+"-"+str((season+1)%100).zfill(2),file)
  try:
   req=urllib.request.Request(url,headers={"User-Agent":"Wettbot/3.0"})
   with urllib.request.urlopen(req,timeout=15) as resp: d=json.load(resp)
   for m in d.get("matches",[]):
    day=m.get("date"); tm=m.get("time") or "15:00"
    if not day: continue
    try:
     from zoneinfo import ZoneInfo
     local=datetime.datetime.fromisoformat(day+"T"+tm).replace(tzinfo=ZoneInfo(zone))
     dt=local.astimezone(datetime.timezone.utc)
    except Exception: continue
    home=(m.get("team1") or {}).get("name"); away=(m.get("team2") or {}).get("name")
    score=m.get("score") or {}; ft=score.get("ft") or score.get("fulltime") or []
    if isinstance(ft,dict): hg=ft.get("team1",ft.get("home")); ag=ft.get("team2",ft.get("away"))
    elif isinstance(ft,list) and len(ft)>=2: hg,ag=ft[0],ft[1]
    else: hg=ag=None
    try:
     hg=int(hg); ag=int(ag)
     if hg>=0 and ag>=0:
      for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
       history.setdefault((league,team),[]).append({"date":dt.isoformat(),"gf":gf,"ga":ga,"result":"W" if gf>ga else "D" if gf==ga else "L","opponent":opponent})
    except (TypeError,ValueError): pass
    if dt<now-datetime.timedelta(hours=4): continue
    all_matches.append({"id":"fallback-"+league.lower().replace(" ","-")+"-"+str(day)+"-"+str(len(all_matches)),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":league,"home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"OpenFootball","dataCoverage":{"fixture":"OpenFootball"}})
  except Exception as e: errors.append(league+": "+str(e)[:100])
 all_matches.sort(key=lambda x:x["timestamp"])
 import math
 for f in all_matches:
  h=sorted(history.get((f["league"],f["home"]),[]),key=lambda x:x["date"])[-5:]
  a=sorted(history.get((f["league"],f["away"]),[]),key=lambda x:x["date"])[-5:]
  f["form"]={"home":h,"away":a}
  f["dataCoverage"]["recentForm"]="OpenFootball-Ergebnisse" if h or a else "Keine historischen Ergebnisse im kostenlosen Feed"
  league_rows=[v for (lg,_),vals in history.items() if lg==f["league"] for v in vals]
  avg=sum(v["gf"] for v in league_rows)/len(league_rows) if league_rows else 1.35
  def avg_goals(rows,key,default):
   vals=[x[key] for x in rows]
   return sum(vals)/len(vals) if vals else default
  lam_h=max(0.15,min(4.0,0.55*avg_goals(h,"gf",avg)+0.45*avg_goals(a,"ga",avg)))
  lam_a=max(0.15,min(4.0,0.55*avg_goals(a,"gf",avg)+0.45*avg_goals(h,"ga",avg)))
  def pois(lam,n): return math.exp(-lam)*lam**n/math.factorial(n)
  probs={"home":0.0,"draw":0.0,"away":0.0}; over25=0.0; btts=0.0; scores=[]
  for i in range(8):
   for j in range(8):
    p=pois(lam_h,i)*pois(lam_a,j); scores.append((p,i,j))
    probs["home" if i>j else "draw" if i==j else "away"]+=p
    if i+j>=3: over25+=p
    if i>0 and j>0: btts+=p
  total=sum(probs.values()) or 1
  probs={k:round(v/total*100,1) for k,v in probs.items()}
  f["prediction"]={"model":"Kostenloses Basis-Modell (letzte verfügbare Ergebnisse)","probabilities":probs,"homeGoals":round(lam_h,2),"awayGoals":round(lam_a,2),"underOver":"Über 2,5" if over25>=0.5 else "Unter 2,5","markets":{"over25":round(over25*100,1),"under25":round((1-over25)*100,1),"bttsYes":round(btts*100,1),"bttsNo":round((1-btts)*100,1)},"mostLikelyScore":"%s:%s"%(max(scores)[1],max(scores)[2])}
  f["analysisStatus"]="model_estimate" if h or a else "limited_model"
  f["dataCoverage"]["prediction"]="Eigene Poisson-Schätzung aus verfügbaren Ergebnissen; nicht validiert"
  f["h2h"]=[x for x in h if x.get("opponent")==f["away"]][-5:]
 enrich_team_news(all_matches,max_teams=40)
 return all_matches,errors
if not KEY:
 fixtures,errors=fixture_fallback()
 save({"generatedAt":now.isoformat(),"status":"free_mode","message":"Kostenloser Modus: OpenFootball-Spielplan und Ergebnisse, eigene einfache Tor-Modellschätzung sowie Google News RSS. Verletzungen und Aufstellungen sind nicht garantiert verfügbar.","recommendations":fixtures,"fixtures":fixtures,"errors":errors,"source":"OpenFootball + Google News RSS + eigenes Poisson-Basismodell"})
 print("Free mode; fixtures=%s with_predictions=%s errors=%s"%(len(fixtures),sum(bool(f.get("prediction")) for f in fixtures),len(errors))); raise SystemExit(0)
season=now.year if now.month>=7 else now.year-1
leagues_api={"Premier League":39,"Bundesliga":78,"La Liga":140,"Serie A":135,"Ligue 1":61,"Primeira Liga":94,"Champions League":2,"Europa League":3,"Conference League":848}
fixtures=[]; errors=[]; start=now.date().isoformat(); end=(now.date()+datetime.timedelta(days=7)).isoformat()
for name,lid in leagues_api.items():
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
for f in fixtures:
 if used>=BUDGET-10: break
 try:
  rows=api("injuries",{"fixture":f["id"]})
  f["injuries"]=[{"player":(x.get("player") or {}).get("name"),"type":(x.get("player") or {}).get("type"),"reason":(x.get("player") or {}).get("reason"),"team":(x.get("team") or {}).get("name")} for x in rows]
  f["dataCoverage"]["injuries"]="API-Football" if rows else "Quelle lieferte keine Einträge"
 except Exception: f["injuries"]=None; f["dataCoverage"]["injuries"]="Nicht abrufbar"
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
