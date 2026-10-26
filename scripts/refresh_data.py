import json, os, datetime, urllib.request, urllib.parse, urllib.error, pathlib, base64, collections, concurrent.futures, time, re, unicodedata
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
# API-Football Free plan is 100 requests/day and 10 requests/minute.
# RamoBet refreshes four times a day: cap one run at 20 requests and preserve quota for retries/manual runs.
BUDGET=20; DAILY_QUOTA_RESERVE=10; used=0; API_DAILY_REMAINING=None; LAST_API_REQUEST=0.0
def save(x): OUT.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding="utf-8")
def api(path,params):
 global used, API_DAILY_REMAINING, LAST_API_REQUEST
 if used>=BUDGET: raise RuntimeError("Run-Budget erreicht (%s API-Anfragen)"%BUDGET)
 if API_DAILY_REMAINING is not None and API_DAILY_REMAINING<=DAILY_QUOTA_RESERVE:
  raise RuntimeError("Tagesquote geschützt: nur noch %s API-Anfragen verfügbar"%API_DAILY_REMAINING)
 # Respect the 10 requests/minute free-plan ceiling, even on rapid workflow retries.
 if LAST_API_REQUEST:
  delay=6.1-(time.monotonic()-LAST_API_REQUEST)
  if delay>0: time.sleep(delay)
 req=urllib.request.Request("https://v3.football.api-sports.io/"+path+"?"+urllib.parse.urlencode(params),headers={"x-apisports-key":KEY,"Accept":"application/json","User-Agent":"RamoBet/3.4"})
 used+=1 # Count attempted requests conservatively, including errors.
 try:
  with urllib.request.urlopen(req,timeout=25) as resp:
   remaining_header=resp.headers.get("x-ratelimit-requests-remaining")
   if remaining_header is not None:
    try: API_DAILY_REMAINING=max(0,int(remaining_header))
    except (TypeError,ValueError): pass
   d=json.load(resp)
  if d.get("errors"): raise RuntimeError(str(d["errors"]))
  result=d.get("response",[])
  if path=="status" and isinstance(result,dict):
   req_info=result.get("requests") or {}
   try:
    body_remaining=max(0,int(req_info.get("limit_day"))-int(req_info.get("current")))
    API_DAILY_REMAINING=body_remaining if API_DAILY_REMAINING is None else min(API_DAILY_REMAINING,body_remaining)
   except (TypeError,ValueError): pass
  return result
 finally:
  LAST_API_REQUEST=time.monotonic()
def fetch_openfootball(season_year, filename):
 season_folder=str(season_year)+"-"+str((season_year+1)%100).zfill(2)
 url="https://api.github.com/repos/openfootball/football.json/contents/"+season_folder+"/"+filename+"?ref=master"
 req=urllib.request.Request(url,headers={"User-Agent":"RamoBet/3.3","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
 with urllib.request.urlopen(req,timeout=18) as resp: entry=json.load(resp)
 if entry.get("encoding")!="base64" or not entry.get("content"):
  raise RuntimeError("OpenFootball GitHub Contents API returned no base64 file content")
 try: return json.loads(base64.b64decode(entry["content"]).decode("utf-8"))
 except Exception as exc: raise RuntimeError("Could not decode OpenFootball season file: "+str(exc)[:100])
from feed_utils import espn_completed_match_rows, full_time_score, match_rows, poisson_match_model
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
 selected=teams[:max_teams]; cache={}
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
  futures={executor.submit(fetch_google_search,'"'+team+'" letzte Spiele Ergebnisse Statistik Fußball',4):team for team in selected}
  for future in concurrent.futures.as_completed(futures):
   team=futures[future]
   try: cache[team]=future.result()
   except Exception: cache[team]=[]
 for f in fixtures:
  f['googleResearch']={'home':cache.get(f.get('home'),[]),'away':cache.get(f.get('away'),[])}
  if f.get('home') not in selected or f.get('away') not in selected:
   note='Nur ein Teil der Teams wurde im Recherchebudget berücksichtigt'
  else:
   note='Google-Suchergebnisse (Links/Snippets, Quelle und Datum vor Verwendung prüfen)'
  f.setdefault('dataCoverage',{})['googleResearch']=note
def enrich_team_news(fixtures, max_teams=24):
 teams=[]
 for f in fixtures:
  for name in (f.get('home'),f.get('away')):
   if name and name not in teams: teams.append(name)
 selected=teams[:max_teams]; cache={}
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
  futures={executor.submit(fetch_team_news,team):team for team in selected}
  for future in concurrent.futures.as_completed(futures):
   team=futures[future]
   try: cache[team]=future.result()
   except Exception: cache[team]=[]
 for f in fixtures:
  f['teamNews']={'home':cache.get(f.get('home'),[]),'away':cache.get(f.get('away'),[])}
  if f.get('home') not in selected or f.get('away') not in selected:
   note='Team wurde aufgrund des Recherchebudgets nicht abgefragt'
  elif f['teamNews']['home'] or f['teamNews']['away']:
   note='Google News RSS-Suchtreffer (nicht automatisch bestätigte Ausfälle)'
  else:
   note='Abfrage erfolgt; keine RSS-Treffer oder Quelle nicht verfügbar'
  f.setdefault('dataCoverage',{})['teamNews']=note
def fetch_espn_events(slug, start_date=None, end_date=None):
 start=start_date or now.date()
 end=end_date or (now.date()+datetime.timedelta(days=WINDOW_DAYS))
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
   # ESPN can reject a range; retry that week day-by-day.
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
 # Recent completed ESPN results fill form gaps, especially for UEFA fixtures and Turkish clubs.
 history_days=45
 history_start=now.date()-datetime.timedelta(days=history_days)
 history_end=now.date()-datetime.timedelta(days=1)
 espn_history_counts={}
 def load_history_for_league(league,slug):
  rows=espn_completed_match_rows(fetch_espn_events(slug,history_start,history_end))
  return league,rows
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
  futures={executor.submit(load_history_for_league,league,slug):league for league,slug in ESPN_SLUGS.items()}
  for future in concurrent.futures.as_completed(futures):
   league=futures[future]
   try:
    _league,completed_rows=future.result()
    added_rows=0
    for row in completed_rows:
     try: dt=datetime.datetime.fromisoformat(row["date"].replace("Z","+00:00"))
     except Exception: continue
     if dt>now or dt<now-datetime.timedelta(days=history_days): continue
     home=row["home"]; away=row["away"]; hg=row["homeGoals"]; ag=row["awayGoals"]
     for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
      history.setdefault((league,team),[]).append({
       "date":dt.isoformat(),"gf":gf,"ga":ga,
       "result":"W" if gf>ga else "D" if gf==ga else "L",
       "opponent":opponent,"home":home,"away":away,
       "homeGoals":hg,"awayGoals":ag,"teamIsHome":team==home,
       "historySource":"ESPN completed scoreboard"
      })
     added_rows+=1
    espn_history_counts[league]=added_rows
   except Exception as e:
    espn_history_counts[league]=0
    errors.append(league+" ESPN history: "+str(e)[:100])
 print("ESPN completed-history matches by league:",json.dumps(dict(sorted(espn_history_counts.items())),ensure_ascii=False))
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
 def last_five(team,league):
  combined={}
  for row in history.get((league,team),[])+history_by_team.get(team_key(team),[]):
   key=(str(row.get("date",""))[:10],team_key(row.get("opponent")),row.get("gf"),row.get("ga"))
   prior=combined.get(key)
   # Prefer precise ESPN kickoff time/source metadata when the two feeds describe the same result.
   if prior is None or row.get("historySource")=="ESPN completed scoreboard":
    combined[key]=row
  return sorted(combined.values(),key=lambda x:x.get("date",""))[-5:]
 # Estimate league home/away goal baselines from unique completed matches, not duplicated team-perspective rows.
 unique_matches_by_league={}
 for (history_league,_team),rows in history.items():
  for row in rows:
   hg=row.get("homeGoals"); ag=row.get("awayGoals")
   if hg is None or ag is None or not row.get("home") or not row.get("away"): continue
   try: hg=int(hg); ag=int(ag)
   except (TypeError,ValueError): continue
   match_key=(str(row.get("date",""))[:10],team_key(row.get("home")),team_key(row.get("away")),hg,ag)
   unique_matches_by_league.setdefault(history_league,{})[match_key]=(hg,ag)
 global_match_map={}
 for games in unique_matches_by_league.values():
  global_match_map.update(games)
 global_games=list(global_match_map.values())
 def goal_baselines(games,default_home=1.45,default_away=1.20):
  if not games: return default_home,default_away
  return (sum(x[0] for x in games)/len(games),sum(x[1] for x in games)/len(games))
 global_home_avg,global_away_avg=goal_baselines(global_games)
 league_baselines={league:goal_baselines(list(games.values()),global_home_avg,global_away_avg) for league,games in unique_matches_by_league.items()}
 for f in all_matches:
  h=last_five(f["home"],f["league"])
  a=last_five(f["away"],f["league"])
  f["form"]={"home":h,"away":a}
  home_sources=sorted({row.get("historySource","OpenFootball season results") for row in h})
  away_sources=sorted({row.get("historySource","OpenFootball season results") for row in a})
  f["dataCoverage"]["recentForm"]="Heim: %s/5; Gast: %s/5; Quellen: %s" % (
   len(h),len(a),", ".join(sorted(set(home_sources+away_sources))) or "keine"
  )
  league_home_avg,league_away_avg=league_baselines.get(f["league"],(global_home_avg,global_away_avg))
  prediction=poisson_match_model(h,a,league_home_avg,league_away_avg)
  winner_key=max(prediction["probabilities"],key=prediction["probabilities"].get)
  prediction["winner"]={"home":f["home"],"draw":"Unentschieden","away":f["away"]}[winner_key]
  prediction["winnerComment"]="Eigene Modellschätzung; keine bestätigte Buchmacher- oder API-Prognose"
  f["prediction"]=prediction
  enough_form=len(h)>=3 and len(a)>=3
  f["analysisStatus"]="model_estimate" if enough_form else "limited_model"
  f["dataCoverage"]["prediction"]=(
   "Recency-weighted Poisson-Modell mit Heim-/Auswärtsraten; Heim %s Historien, Gast %s Historien; nicht extern validiert" % (len(h),len(a))
   if enough_form else
   "Begrenzte Modellbasis: Heim %s/5 und Gast %s/5 historische Ergebnisse; Liga-Durchschnitt gleicht fehlende Werte teilweise aus" % (len(h),len(a))
  )
  f["marketCoverage"]={"1X2":"Poisson-Modell aus jüngster Teamform und Liga-Heim-/Auswärtsschnitt","overUnder":"Poisson-Torverteilung","btts":"Poisson-Torverteilung","corners":"Keine belastbare kostenlose Prognose verfügbar","cards":"Keine belastbare kostenlose Prognose verfügbar","playerProps":"Keine belastbare kostenlose Prognose verfügbar"}
  f["h2h"]=sorted([x for x in history_by_team.get(team_key(f["home"]),[]) if team_key(x.get("opponent"))==team_key(f["away"])],key=lambda x:x.get("date",""))[-5:]
 enrich_team_news(all_matches,max_teams=80)
 enrich_google_team_research(all_matches,max_teams=40)
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
   row_key=(str(row.get("date",""))[:10],row.get("opponent"),row.get("gf"),row.get("ga"))
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
