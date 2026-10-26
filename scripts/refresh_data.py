import json, os, datetime, urllib.request, urllib.parse, urllib.error, pathlib, base64, collections
KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
ROOT=pathlib.Path(__file__).resolve().parent.parent
CONFIG=json.loads((ROOT/"competitions.json").read_text(encoding="utf-8"))
COMPETITIONS=CONFIG["competitions"]
if len(COMPETITIONS)!=10 or len({x.get("name") for x in COMPETITIONS})!=10:
 raise SystemExit("competitions.json must contain exactly 10 uniquely named configured competitions")
OUT=ROOT/"recommendations.json"; now=datetime.datetime.now(datetime.timezone.utc)
season=now.year if now.month>=6 else now.year-1
WINDOW_DAYS=max(1,min(60,int(CONFIG.get("fixtureWindowDays",7))))
leagues={x["name"]:(x["openFootballFile"],x["openFootballTimezone"]) for x in COMPETITIONS if x.get("openFootballFile") and x.get("openFootballTimezone")}
ESPN_SLUGS={x["name"]:x["espnSlug"] for x in COMPETITIONS if x.get("espnSlug")}
API_LEAGUE_IDS={x["name"]:x["apiFootballId"] for x in COMPETITIONS if x.get("apiFootballId") is not None}
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
def fetch_openfootball(season_year, filename):
 season_folder=str(season_year)+"-"+str((season_year+1)%100).zfill(2)
 url="https://api.github.com/repos/openfootball/football.json/contents/"+season_folder+"/"+filename+"?ref=master"
 req=urllib.request.Request(url,headers={"User-Agent":"RamoBet/3.3","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
 with urllib.request.urlopen(req,timeout=18) as resp: entry=json.load(resp)
 if entry.get("encoding")!="base64" or not entry.get("content"):
  raise RuntimeError("OpenFootball GitHub Contents API returned no base64 file content")
 try: return json.loads(base64.b64decode(entry["content"]).decode("utf-8"))
 except Exception as exc: raise RuntimeError("Could not decode OpenFootball season file: "+str(exc)[:100])
from feed_utils import match_rows, full_time_score
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
def fetch_google_search(query, limit=5):
 # Google web search is used as a link/snippet discovery source; results are not treated as verified structured statistics.
 url='https://www.google.com/search?'+urllib.parse.urlencode({'q':query,'hl':'de','num':str(limit)})
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36','Accept-Language':'de-DE,de;q=0.9,en;q=0.7'})
  with urllib.request.urlopen(req,timeout=12) as resp: html=resp.read().decode('utf-8','ignore')
  from html.parser import HTMLParser
  import re, html as htmlmod
  class SearchParser(HTMLParser):
   def __init__(self):
    super().__init__(); self.results=[]; self.current=None; self.capture_title=False; self.capture_snippet=False; self.depth=0
   def handle_starttag(self,tag,attrs):
    a=dict(attrs)
    if tag=='a' and a.get('href','').startswith('http') and 'google.com' not in a.get('href','') and self.current is None:
     self.current={'url':a['href'],'title':'','snippet':''}; self.capture_title=False; self.depth=0
    elif self.current is not None and tag=='h3': self.capture_title=True
    elif self.current is not None and tag in ('div','span') and a.get('data-sncf')=='1': self.capture_snippet=True
   def handle_data(self,data):
    if self.current is not None:
     val=data.strip()
     if val and self.capture_title: self.current['title']+=((' ' if self.current['title'] else '')+val)
     elif val and self.capture_snippet and len(self.current['snippet'])<500: self.current['snippet']+=((' ' if self.current['snippet'] else '')+val)
   def handle_endtag(self,tag):
    if tag=='h3': self.capture_title=False
    if tag=='a' and self.current is not None:
     if self.current.get('title') and self.current['url'].startswith('https://'):
      self.results.append(self.current)
     self.current=None; self.capture_title=False; self.capture_snippet=False
  parser=SearchParser(); parser.feed(html)
  out=[]; seen=set()
  for item in parser.results:
   if item['url'] in seen: continue
   seen.add(item['url'])
   item['title']=htmlmod.unescape(item['title']).strip()
   item['snippet']=htmlmod.unescape(item['snippet']).strip()
   out.append(item)
   if len(out)>=limit: break
  return out
 except Exception:
  return []
def enrich_google_team_research(fixtures, max_teams=20):
 teams=[]
 for f in fixtures:
  for team in (f.get('home'),f.get('away')):
   if team and team not in teams: teams.append(team)
 attempted=set(teams[:max_teams]); cache={}
 for team in attempted:
  cache[team]=fetch_google_search('"'+team+'" letzte Spiele Ergebnisse Statistik Fußball',4)
 for f in fixtures:
  f['googleResearch']={'home':cache.get(f.get('home'),[]),'away':cache.get(f.get('away'),[])}
  if f.get('home') not in attempted or f.get('away') not in attempted:
   note='Nur ein Teil der Teams wurde im Recherchebudget berücksichtigt'
  else:
   note='Google-Suchergebnisse (Links/Snippets, Quelle und Datum vor Verwendung prüfen)'
  f.setdefault('dataCoverage',{})['googleResearch']=note
def enrich_team_news(fixtures, max_teams=24):
 teams=[]
 for f in fixtures:
  for name in (f.get('home'),f.get('away')):
   if name and name not in teams: teams.append(name)
 attempted=set(teams[:max_teams]); cache={}
 for team in attempted:
  cache[team]=fetch_team_news(team)
 for f in fixtures:
  f['teamNews']={'home':cache.get(f.get('home'),[]),'away':cache.get(f.get('away'),[])}
  if f.get('home') not in attempted or f.get('away') not in attempted:
   note='Team wurde aufgrund des Recherchebudgets nicht abgefragt'
  elif f['teamNews']['home'] or f['teamNews']['away']:
   note='Google News RSS-Suchtreffer (nicht automatisch bestätigte Ausfälle)'
  else:
   note='Abfrage erfolgt; keine RSS-Treffer oder Quelle nicht verfügbar'
  f.setdefault('dataCoverage',{})['teamNews']=note
def fetch_espn_events(slug):
 start=now.date()
 end=now.date()+datetime.timedelta(days=WINDOW_DAYS)
 found={}
 cursor=start
 headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36","Accept":"application/json","Referer":"https://www.espn.com/"}
 def request_events(date_arg):
  url="https://site.api.espn.com/apis/site/v2/sports/soccer/"+slug+"/scoreboard?dates="+date_arg
  req=urllib.request.Request(url,headers=headers)
  with urllib.request.urlopen(req,timeout=10) as resp: return json.load(resp).get("events",[])
 while cursor<=end:
  chunk_end=min(end,cursor+datetime.timedelta(days=6))
  date_arg=cursor.strftime("%Y%m%d")+"-"+chunk_end.strftime("%Y%m%d")
  try:
   batch=request_events(date_arg)
  except urllib.error.HTTPError as exc:
   if exc.code!=400: raise
   # ESPN sometimes rejects date ranges; retry those dates individually.
   batch=[]
   day=cursor
   while day<=chunk_end:
    batch.extend(request_events(day.strftime("%Y%m%d")))
    day+=datetime.timedelta(days=1)
  for event in batch:
   event_id=str(event.get("id") or "")
   if event_id: found[event_id]=event
  cursor=chunk_end+datetime.timedelta(days=1)
 return list(found.values())
def fixture_fallback():
 all_matches=[]; errors=[]; history={}
 for league,(file,zone) in leagues.items():
  try:
   d=fetch_openfootball(season,file)
   current_rows=match_rows(d)
   print("OpenFootball current season:",league,"matches=",len(current_rows),"root_type=",type(d).__name__)
   for m in current_rows:
    day=m.get("date"); tm=m.get("time") or "15:00"
    if not day: continue
    try:
     from zoneinfo import ZoneInfo
     local=datetime.datetime.fromisoformat(day+"T"+tm).replace(tzinfo=ZoneInfo(zone))
     dt=local.astimezone(datetime.timezone.utc)
    except Exception: continue
    t1=m.get("team1"); t2=m.get("team2")
    home=(t1.get("name") if isinstance(t1,dict) else t1 if isinstance(t1,str) else None)
    away=(t2.get("name") if isinstance(t2,dict) else t2 if isinstance(t2,str) else None)
    if not home or not away: continue
    score=m.get("score")
    hg,ag=full_time_score(score)
    try:
     hg=int(hg); ag=int(ag)
     if hg>=0 and ag>=0:
      for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
       history.setdefault((league,team),[]).append({"date":dt.isoformat(),"gf":gf,"ga":ga,"result":"W" if gf>ga else "D" if gf==ga else "L","opponent":opponent,"home":home,"away":away,"homeGoals":hg,"awayGoals":ag,"teamIsHome":team==home})
    except (TypeError,ValueError): pass
    if dt<=now or dt>now+datetime.timedelta(days=WINDOW_DAYS): continue
    all_matches.append({"id":"fallback-"+league.lower().replace(" ","-")+"-"+str(day)+"-"+str(len(all_matches)),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":league,"home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"OpenFootball","dataCoverage":{"fixture":"OpenFootball"}})
  except Exception as e:
   errors.append(league+" OpenFootball: "+str(e)[:80]) # Supplement current-season results with previous-season results for a stable form baseline.
 previous_season=season-1
 for league,(file,zone) in leagues.items():
  try:
   previous_data=fetch_openfootball(previous_season,file)
   from zoneinfo import ZoneInfo
   for m in match_rows(previous_data):
    day=m.get("date"); tm=m.get("time") or "15:00"
    if not day: continue
    try: dt=datetime.datetime.fromisoformat(day+"T"+tm).replace(tzinfo=ZoneInfo(zone)).astimezone(datetime.timezone.utc)
    except Exception: continue
    if dt>now: continue
    t1=m.get("team1"); t2=m.get("team2")
    home=(t1.get("name") if isinstance(t1,dict) else t1 if isinstance(t1,str) else None)
    away=(t2.get("name") if isinstance(t2,dict) else t2 if isinstance(t2,str) else None)
    if not home or not away: continue
    score=m.get("score")
    hg,ag=full_time_score(score)
    if hg is None or ag is None: continue
    try: hg=int(hg); ag=int(ag)
    except (TypeError,ValueError): continue
    if hg<0 or ag<0: continue
    for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
     rows=history.setdefault((league,team),[])
     if not any(x.get("date")==dt.isoformat() and x.get("opponent")==opponent for x in rows):
      rows.append({"date":dt.isoformat(),"gf":gf,"ga":ga,"result":"W" if gf>ga else "D" if gf==ga else "L","opponent":opponent,"home":home,"away":away,"homeGoals":hg,"awayGoals":ag,"teamIsHome":team==home})
  except Exception as e:
   errors.append(league+" previous-season history: "+str(e)[:70])
 # Secondary independent fixture feed: ESPN for every configured competition.
 for league,slug in ESPN_SLUGS.items():
  try:
   for event in fetch_espn_events(slug):
    comps=event.get("competitions") or []
    if not comps: continue
    competitors=comps[0].get("competitors") or []
    home=next((z.get("team",{}).get("displayName") for z in competitors if z.get("homeAway")=="home"),None)
    away=next((z.get("team",{}).get("displayName") for z in competitors if z.get("homeAway")=="away"),None)
    rawdate=event.get("date")
    if not home or not away or not rawdate: continue
    dt=datetime.datetime.fromisoformat(rawdate.replace("Z","+00:00"))
    if dt<=now or dt>now+datetime.timedelta(days=WINDOW_DAYS): continue
    all_matches.append({"id":"espn-"+str(event.get("id")),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":league,"home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"ESPN public scoreboard","dataCoverage":{"fixture":"ESPN public scoreboard"}})
  except Exception as e:
   errors.append(league+" ESPN fallback: "+str(e)[:100])
 # OpenLigaDB is an additional keyless source for the German Bundesliga.
 try:
  req=urllib.request.Request("https://api.openligadb.de/getmatchdata/bl1",headers={"User-Agent":"RamoBet/1.0","Accept":"application/json"})
  with urllib.request.urlopen(req,timeout=8) as resp: matches=json.load(resp)
  for m in matches:
   rawdate=m.get("matchDateTimeUTC"); home=(m.get("team1") or {}).get("teamName"); away=(m.get("team2") or {}).get("teamName")
   if not rawdate or not home or not away: continue
   dt=datetime.datetime.fromisoformat(rawdate.replace("Z","+00:00"))
   if dt<=now or dt>now+datetime.timedelta(days=WINDOW_DAYS): continue
   all_matches.append({"id":"openligadb-"+str(m.get("matchID")),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":"Bundesliga","home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"OpenLigaDB","dataCoverage":{"fixture":"OpenLigaDB"}})
 except Exception as e:
  errors.append("OpenLigaDB Bundesliga: "+str(e)[:80])
 # Canonical team names before source merging; this removes ESPN/OpenFootball alias duplicates.
 import unicodedata,re,math
 aliases={
  "fcbarcelona":"barcelona","barcelonafc":"barcelona",
  "bayernmunchen":"bayernmunich","fcbayernmunchen":"bayernmunich","fcbayern":"bayernmunich",
  "fcinternazionalemilano":"intermilan","internazionalemilano":"intermilan","internazionale":"intermilan","inter":"intermilan",
  "milan":"acmilan","acmilan":"acmilan","sscnapoli":"napoli","napoli":"napoli","juventusfc":"juventus",
  "parissaintgermain":"psg","parissaintgermainfc":"psg","galatasarayistanbul":"galatasaray","galatasaraysk":"galatasaray",
  "fenerbahceistanbul":"fenerbahce","fenerbahcesk":"fenerbahce","besiktasistanbul":"besiktas","besiktasjk":"besiktas",
  "fcporto":"porto","sportingcp":"sportinglisbon","sportingclubedeportugal":"sportinglisbon","sportinglisbon":"sportinglisbon",
  "athleticclub":"athleticbilbao","athleticbilbao":"athleticbilbao","realbetisseville":"realbetis",
  "atleticodemadrid":"atleticomadrid","realmadridcf":"realmadrid","realracingclubdesantander":"racingdesantander",
  "arsenalfc":"arsenal","liverpoolfc":"liverpool","chelseafc":"chelsea","manchesterunitedfc":"manchesterunited",
  "manchestercityfc":"manchestercity","tottenhamhotspurfc":"tottenhamhotspur","evertonfc":"everton",
  "westhamunitedfc":"westhamunited","newcastleunitedfc":"newcastleunited","crystalpalacefc":"crystalpalace",
  "astonvillafc":"astonvilla","brightonandhovealbionfc":"brighton",
  "olympiquedemarseille":"marseille","olympiquelyonnais":"lyon","asmonaco":"monaco","lilleosc":"lille",
  "fcfamalicao":"famalicao","sportingclube":"sportinglisbon"
 }
 def team_key(value):
  key=re.sub(r"[^a-z0-9]","",unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode().lower())
  return aliases.get(key,key)
 unique=[]; seen=set(); duplicate_sources=collections.Counter()
 for match in sorted(all_matches,key=lambda x:x.get("timestamp",0)):
  home_key=team_key(match.get("home")); away_key=team_key(match.get("away"))
  pair=tuple(sorted((home_key,away_key)))
  key=(match.get("league"),match.get("date","")[:10],pair)
  if key in seen:
   duplicate_sources[match.get("source","unknown")]+=1
   continue
  seen.add(key); unique.append(match)
 print("Duplicate fixtures removed by source:",json.dumps(dict(sorted(duplicate_sources.items())),ensure_ascii=False))
 all_matches=unique
 all_matches.sort(key=lambda x:x["timestamp"])
 history_by_team={}
 for (history_league,team),rows in history.items():
  history_by_team.setdefault(team_key(team),[]).extend(rows)
 all_history_rows=[row for rows in history.values() for row in rows]
 def last_five(team,league):
  combined={}
  for row in history.get((league,team),[])+history_by_team.get(team_key(team),[]):
   key=(row.get("date"),team_key(row.get("opponent")),row.get("gf"),row.get("ga"))
   combined[key]=row
  return sorted(combined.values(),key=lambda x:x["date"])[-5:]
 for f in all_matches:
  h=last_five(f["home"],f["league"])
  a=last_five(f["away"],f["league"])
  f["form"]={"home":h,"away":a}
  f["dataCoverage"]["recentForm"]="OpenFootball-Ergebnisse" if h or a else "Keine historischen Ergebnisse im kostenlosen Feed"
  league_rows=[v for (lg,_),vals in history.items() if lg==f["league"] for v in vals]
  if not league_rows: league_rows=all_history_rows
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
  winner_key=max(probs,key=probs.get)
  predicted_winner={"home":f["home"],"draw":"Unentschieden","away":f["away"]}[winner_key]
  f["prediction"]={"model":"Poisson-Basismodell aus letzten verfügbaren Team-Ergebnissen","winner":predicted_winner,"winnerComment":"Modellschätzung, keine bestätigte Marktprognose","probabilities":probs,"homeGoals":round(lam_h,2),"awayGoals":round(lam_a,2),"underOver":"Über 2,5" if over25>=0.5 else "Unter 2,5","markets":{"over25":round(over25*100,1),"under25":round((1-over25)*100,1),"bttsYes":round(btts*100,1),"bttsNo":round((1-btts)*100,1)},"mostLikelyScore":"%s:%s"%(max(scores,key=lambda z:z[0])[1],max(scores,key=lambda z:z[0])[2])}
  f["analysisStatus"]="model_estimate" if h or a else "limited_model"
  f["dataCoverage"]["prediction"]="Eigene Poisson-Schätzung aus letzten Team-Ergebnissen; nicht extern validiert" if h or a else "Begrenzte Basisschätzung: keine passenden Team-Ergebnisse gefunden"
  f["marketCoverage"]={"1X2":"Poisson-Modell aus Team-Ergebnissen","overUnder":"Poisson-Torverteilung","btts":"Poisson-Torverteilung","corners":"Keine belastbare kostenlose Prognose verfügbar","cards":"Keine belastbare kostenlose Prognose verfügbar","playerProps":"Keine belastbare kostenlose Prognose verfügbar"}
  f["h2h"]=sorted([x for x in history_by_team.get(team_key(f["home"]),[]) if team_key(x.get("opponent"))==team_key(f["away"])],key=lambda x:x["date"])[-5:]
 enrich_team_news(all_matches,max_teams=40)
 enrich_google_team_research(all_matches,max_teams=20)
 print("Fallback fixture counts by league:",json.dumps(dict(sorted(collections.Counter(x.get("league","UNKNOWN") for x in all_matches).items())),ensure_ascii=False))
 print("Fallback fixture counts by source:",json.dumps(dict(sorted(collections.Counter(x.get("source","unknown") for x in all_matches).items())),ensure_ascii=False))
 status_by_league={}
 date_bounds={}
 for fixture in all_matches:
  league_name=fixture.get("league","UNKNOWN")
  status_by_league.setdefault(league_name,collections.Counter())[fixture.get("analysisStatus","unknown")]+=1
  date_value=fixture.get("date")
  if date_value:
   date_obj=datetime.datetime.fromisoformat(date_value.replace("Z","+00:00"))
   bounds=date_bounds.setdefault(league_name,{"first":date_obj,"last":date_obj})
   if date_obj<bounds["first"]: bounds["first"]=date_obj
   if date_obj>bounds["last"]: bounds["last"]=date_obj
 print("Analysis status by league:",json.dumps({k:dict(v) for k,v in sorted(status_by_league.items())},ensure_ascii=False))
 print("Fixture date bounds by league:",json.dumps({k:{"count":status_by_league[k].total(),"first":v["first"].isoformat(),"last":v["last"].isoformat()} for k,v in sorted(date_bounds.items())},ensure_ascii=False))
 print("Source errors:",json.dumps(errors[:30],ensure_ascii=False))
 # Store a compact, normalized team-history index so browser-side ESPN fallback fixtures can also be modeled.
 compact_history={}
 for key,rows in history_by_team.items():
  unique={}
  for row in rows:
   row_key=(row.get("date"),row.get("opponent"),row.get("gf"),row.get("ga"))
   unique[row_key]=row
  compact_history[key]=sorted(unique.values(),key=lambda x:x.get("date",""))[-10:]
 return all_matches,errors,compact_history
if not KEY:
 fixtures,errors,team_history=fixture_fallback()
 save({"generatedAt":now.isoformat(),"status":"free_mode","message":"Kostenloser Modus: feste 10-Ligen-Konfiguration, OpenFootball/ESPN/OpenLigaDB-Spielpläne, Team-Ergebnisse und Poisson-Modell. Verletzungen, Aufstellungen und Quoten sind nicht für jedes Spiel verfügbar.","configuredLeagues":[x["name"] for x in COMPETITIONS],"fixtureWindowDays":WINDOW_DAYS,"teamHistory":team_history,"recommendations":fixtures,"fixtures":fixtures,"errors":errors,"source":"OpenFootball + ESPN + OpenLigaDB + Google News RSS + Google-Suche + Poisson-Basismodell"})
 print("Free mode; fixtures=%s with_predictions=%s errors=%s"%(len(fixtures),sum(bool(f.get("prediction")) for f in fixtures),len(errors))); raise SystemExit(0)
season=now.year if now.month>=7 else now.year-1
leagues_api=API_LEAGUE_IDS
fixtures=[]; errors=[]; start=now.date().isoformat(); end=(now.date()+datetime.timedelta(days=WINDOW_DAYS)).isoformat()
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
fixtures.sort(key=lambda x:x["timestamp"])
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
save({"generatedAt":now.isoformat(),"status":"ok","message":"Alle Spiele im konfigurierten Zeitraum werden geladen; verfügbare Prognosen und Datenabdeckung sind pro Spiel gekennzeichnet.","configuredLeagues":[x["name"] for x in COMPETITIONS],"fixtureWindowDays":WINDOW_DAYS,"recommendations":fixtures,"fixtures":fixtures,"errors":errors[:25],"source":"API-Football","requestsUsed":used,"dailyRequestBudget":BUDGET,"season":season})
print("fixtures=%s predictions=%s requests=%s errors=%s"%(len(fixtures),sum(bool(f.get("prediction")) for f in fixtures),used,len(errors)))
