import json, os, datetime, urllib.request, urllib.parse, urllib.error, pathlib, base64, collections, concurrent.futures, time, re, unicodedata
KEY=os.environ.get("API_FOOTBALL_KEY","").strip()
FOOTBALL_DATA_TOKEN=os.environ.get("FOOTBALL_DATA_TOKEN","").strip()
ROOT=pathlib.Path(__file__).resolve().parent.parent
CONFIG=json.loads((ROOT/"competitions.json").read_text(encoding="utf-8"))
COMPETITIONS=CONFIG["competitions"]
if len(COMPETITIONS)!=10 or len({x.get("name") for x in COMPETITIONS})!=10:
 raise SystemExit("competitions.json must contain exactly 10 uniquely named configured competitions")
OUT=ROOT/"recommendations.json"; now=datetime.datetime.now(datetime.timezone.utc)
TEAM_RESEARCH_CACHE={"news":{},"googleSearch":{}}; TEAM_RESEARCH_ERRORS=[]
season=now.year if now.month>=6 else now.year-1
WINDOW_DAYS=max(1,min(60,int(CONFIG.get("fixtureWindowDays",7))))
leagues={x["name"]:(x["openFootballFile"],x["openFootballTimezone"]) for x in COMPETITIONS if x.get("openFootballFile") and x.get("openFootballTimezone")}
ESPN_SLUGS={x["name"]:x["espnSlug"] for x in COMPETITIONS if x.get("espnSlug")}
API_LEAGUE_IDS={x["name"]:x["apiFootballId"] for x in COMPETITIONS if x.get("apiFootballId") is not None}
# API-Football Free plan is 100 requests/day and 10 requests/minute.
# RamoBet refreshes four times a day: cap one run at 20 requests and preserve quota for retries/manual runs.
BUDGET=20; DAILY_QUOTA_RESERVE=10; used=0; API_DAILY_REMAINING=None; LAST_API_REQUEST=0.0
def save(x):
 x.setdefault("teamResearchCache",TEAM_RESEARCH_CACHE)
 x.setdefault("researchErrors",TEAM_RESEARCH_ERRORS[-100:])
 x.setdefault("researchSummary",{kind:{"teamsCached":len(items),"cachedErrors":sum(1 for v in items.values() if v.get("status")=="error")} for kind,items in TEAM_RESEARCH_CACHE.items()})
 OUT.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding="utf-8")
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
from feed_utils import espn_completed_match_rows, full_time_score, match_rows, poisson_match_model, sportscore_match_rows
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
 except Exception:
  return None
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
  return None
def research_cache_fresh(entry,ttl_hours):
 if not isinstance(entry,dict) or not isinstance(entry.get("items"),list): return False
 if entry.get("status")=="error":
  try:
   retry=datetime.datetime.fromisoformat(str(entry.get("retryAfterAt","")).replace("Z","+00:00"))
   if retry.tzinfo is None: retry=retry.replace(tzinfo=datetime.timezone.utc)
   return now<retry
  except Exception:
   return False
 if not entry.get("updatedAt"): return False
 try:
  stamp=datetime.datetime.fromisoformat(str(entry["updatedAt"]).replace("Z","+00:00"))
  if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=datetime.timezone.utc)
  age=now-stamp
  return datetime.timedelta(0)<=age<=datetime.timedelta(hours=ttl_hours)
 except Exception:
  return False
def store_research_result(kind,team,items):
 cache=TEAM_RESEARCH_CACHE.setdefault(kind,{})
 if isinstance(items,list):
  entry={"items":items,"updatedAt":now.isoformat(),"status":"ok"}
 else:
  previous=cache.get(team,{})
  retry=(now+datetime.timedelta(hours=2)).isoformat()
  entry={"items":previous.get("items",[]) if isinstance(previous,dict) else [],
         "updatedAt":previous.get("updatedAt") if isinstance(previous,dict) else None,
         "status":"error","lastAttemptAt":now.isoformat(),"retryAfterAt":retry}
  TEAM_RESEARCH_ERRORS.append({"source":kind,"team":team,"error":"source request failed","at":now.isoformat()})
 cache[team]=entry
 return entry
def team_names(fixtures):
 names=[]
 for fixture in fixtures:
  for name in (fixture.get("home"),fixture.get("away")):
   if name and name not in names: names.append(name)
 return names
def enrich_google_team_research(fixtures, max_teams=40):
 teams=team_names(fixtures); cache=TEAM_RESEARCH_CACHE.setdefault("googleSearch",{})
 stale=[team for team in teams if not research_cache_fresh(cache.get(team),24)]
 selected=stale[:max_teams]
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
  futures={executor.submit(fetch_google_search, '"'+team+'" letzte Spiele Ergebnisse Statistik Fußball', 4):team for team in selected}
  for future in concurrent.futures.as_completed(futures):
   team=futures[future]
   try: items=future.result()
   except Exception: items=None
   store_research_result("googleSearch",team,items)
 for fixture in fixtures:
  home_entry=cache.get(fixture.get("home"),{}); away_entry=cache.get(fixture.get("away"),{})
  fixture["googleResearch"]={"home":home_entry.get("items",[]),"away":away_entry.get("items",[])}
  if home_entry.get("status")=="error" or away_entry.get("status")=="error":
   note="Google-Suche momentan nicht erreichbar; vorhandene Treffer können älter sein"
  elif fixture.get("home") in stale or fixture.get("away") in stale:
   note="Google-Suche mit Team-Cache; einzelne Teams werden laufend ergänzt"
  else:
   note="Google-Suchergebnisse aus frischem Team-Cache (Links/Snippets, keine bestätigten Statistiken)"
  fixture.setdefault("dataCoverage",{})["googleResearch"]=note
def enrich_team_news(fixtures, max_teams=None):
 teams=team_names(fixtures); cache=TEAM_RESEARCH_CACHE.setdefault("news",{})
 stale=[team for team in teams if not research_cache_fresh(cache.get(team),12)]
 with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
  futures={executor.submit(fetch_team_news,team):team for team in stale}
  for future in concurrent.futures.as_completed(futures):
   team=futures[future]
   try: items=future.result()
   except Exception: items=None
   store_research_result("news",team,items)
 for fixture in fixtures:
  home_entry=cache.get(fixture.get("home"),{}); away_entry=cache.get(fixture.get("away"),{})
  fixture["teamNews"]={"home":home_entry.get("items",[]),"away":away_entry.get("items",[])}
  fresh_home=research_cache_fresh(home_entry,12); fresh_away=research_cache_fresh(away_entry,12)
  if home_entry.get("status")=="error" or away_entry.get("status")=="error":
   note="Google News RSS momentan nicht erreichbar; vorhandene Treffer können älter sein"
  elif not fresh_home or not fresh_away:
   note="Team-News-Cache teilweise älter; Abruf wird im nächsten Aktualisierungslauf ergänzt"
  elif fixture["teamNews"]["home"] or fixture["teamNews"]["away"]:
   note="Google News RSS (Treffer sind Hinweise, keine automatisch bestätigten Ausfälle)"
  else:
   note="Beide Team-Abfragen aktuell; keine RSS-Treffer geliefert"
  fixture.setdefault("dataCoverage",{})["teamNews"]=note
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
 SOURCE_DIAGNOSTICS.clear()
 openfootball_fixture_counts=collections.Counter()
 openfootball_history_counts=collections.Counter()
 for league,(file,zone) in leagues.items():
  try:
   d=fetch_openfootball(season,file)
   current_rows=match_rows(d)
   print("OpenFootball current season:",league,"matches=",len(current_rows),"root_type=",type(d).__name__)
   parsed_count=0
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
    parsed_count+=1
   openfootball_fixture_counts[league]=parsed_count
   _diag("OpenFootball","ok" if current_rows else "empty",True,parsed_count,league=league,seasonRows=len(current_rows),season=season)
  except Exception as e:
   errors.append(league+" OpenFootball: "+str(e)[:80])
   _diag("OpenFootball","error",False,0,str(e),league=league) # Supplement current-season results with previous-season results for a stable form baseline.
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
 espn_fixture_counts=collections.Counter()
 espn_errors=[]
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
    all_matches.append({"id":"espn-"+str(event.get("id")),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":league,"home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"ESPN","dataCoverage":{"fixture":"ESPN public scoreboard"}})
    espn_fixture_counts[league]+=1
  except Exception as e:
   espn_errors.append(league+": "+str(e)[:100])
   errors.append(league+" ESPN fallback: "+str(e)[:100])
 _diag("ESPN","error" if espn_errors else "ok" if sum(espn_fixture_counts.values()) else "empty",not bool(espn_errors),sum(espn_fixture_counts.values()),"; ".join(espn_errors[:10]) or None,perLeague=dict(espn_fixture_counts),requestedLeagues=len(ESPN_SLUGS),failedLeagues=len(espn_errors))
 # Recent completed ESPN results fill form gaps, especially for UEFA fixtures and Turkish clubs.
 history_days=90
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
  count=0
  for m in matches if isinstance(matches,list) else []:
   rawdate=m.get("matchDateTimeUTC"); home=(m.get("team1") or {}).get("teamName"); away=(m.get("team2") or {}).get("teamName")
   if not rawdate or not home or not away: continue
   dt=datetime.datetime.fromisoformat(rawdate.replace("Z","+00:00"))
   if dt<=now or dt>now+datetime.timedelta(days=WINDOW_DAYS): continue
   all_matches.append({"id":"openligadb-"+str(m.get("matchID")),"date":dt.isoformat(),"timestamp":dt.timestamp(),"league":"Bundesliga","home":home,"away":away,"venue":None,"status":"scheduled","analysisStatus":"fixture_only","prediction":{},"h2h":[],"source":"OpenLigaDB","dataCoverage":{"fixture":"OpenLigaDB"}})
   count+=1
  _diag("OpenLigaDB","ok" if count else "empty",True,count,responseRows=len(matches) if isinstance(matches,list) else 0)
 except Exception as e:
  errors.append("OpenLigaDB Bundesliga: "+str(e)[:80])
  _diag("OpenLigaDB","error",False,0,str(e))
 # Independent SportScore source: daily fixture/result endpoint, separate from ESPN.
 # One request per calendar day covers all competitions; restrict output to RamoBet's fixed list.
 def sportscore_competition(value):
  import re
  import unicodedata
  norm=re.sub(r"[^a-z0-9]","",unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode().lower())
  aliases={
   "bundesliga":"Bundesliga","germanbundesliga":"Bundesliga","1bundesliga":"Bundesliga","germanybundesliga":"Bundesliga",
   "seriea":"Serie A","italianseriea":"Serie A","serieaitaly":"Serie A",
   "ligue1":"Ligue 1","frenchligue1":"Ligue 1",
   "laliga":"La Liga","laligaspain":"La Liga","spanishlaliga":"La Liga","primeradivision":"La Liga",
   "premierleague":"Premier League","englishpremierleague":"Premier League","epl":"Premier League",
   "superlig":"Süper Lig","turkishsuperlig":"Süper Lig","superligturkey":"Süper Lig","turkeysuperlig":"Süper Lig",
   "primeiraliga":"Primeira Liga","portugueseprimeiraliga":"Primeira Liga","ligaportugal":"Primeira Liga","portugalprimeiraliga":"Primeira Liga",
   "championsleague":"Champions League","uefachampionsleague":"Champions League",
   "europaleague":"Europa League","uefaeuropaleague":"Europa League",
   "conferenceleague":"Conference League","uefaconferenceleague":"Conference League","uefaeuropaconferenceleague":"Conference League","europaconferenceleague":"Conference League"
  }
  return aliases.get(norm)
 def sportscore_fetch_day(day):
  params=urllib.parse.urlencode({"sport":"football","date":day.isoformat(),"limit":200,"src":"ramobet-ios"})
  url="https://sportscore.com/api/v1/fixtures/?"+params
  req=urllib.request.Request(url,headers={"User-Agent":"RamoBet/1.0 (+https://ramikisa.github.io/wettbot-ios/)","Accept":"application/json"})
  with urllib.request.urlopen(req,timeout=12) as resp:
   if not 200<=resp.status<300: raise RuntimeError("HTTP "+str(resp.status))
   payload=json.load(resp)
  return day,sportscore_match_rows(payload)
 sportscore_fixture_counts=collections.Counter()
 sportscore_history_counts=collections.Counter()
 sportscore_errors=[]
 sportscore_http_success=0
 sportscore_http_empty=0
 sportscore_days=[]
 for offset in range(-45,WINDOW_DAYS+1):
  sportscore_days.append(now.date()+datetime.timedelta(days=offset))
 def fetch_sportscore_safe(day):
  try: return day,sportscore_fetch_day(day)[1],None
  except Exception as exc: return day,[],str(exc)[:120]
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
  for day,rows,error in pool.map(fetch_sportscore_safe,sportscore_days):
   if error:
    sportscore_errors.append(day.isoformat()+": "+error)
    continue
   sportscore_http_success+=1
   if not rows: sportscore_http_empty+=1
   for row in rows:
    league=sportscore_competition(row.get("competition"))
    if not league: continue
    rawdate=row.get("date")
    try:
     kickoff=datetime.datetime.fromisoformat(str(rawdate).replace("Z","+00:00"))
     if kickoff.tzinfo is None:
      from zoneinfo import ZoneInfo
      try: kickoff=kickoff.replace(tzinfo=ZoneInfo(str(row.get("timezone") or "UTC")))
      except Exception: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
     kickoff=kickoff.astimezone(datetime.timezone.utc)
    except Exception:
     continue
    home=row["home"]; away=row["away"]
    status_norm=re.sub(r"[^a-z]","",str(row.get("status") or "").lower())
    is_finished=status_norm in {"finished","finish","complete","completed","post","ft","fulltime","ended","final"}
    is_cancelled=any(term in status_norm for term in ("cancel","postpon","abandon","suspend"))
    if is_finished and row.get("homeGoals") is not None and row.get("awayGoals") is not None:
     if kickoff>now or kickoff<now-datetime.timedelta(days=45): continue
     hg=row["homeGoals"]; ag=row["awayGoals"]
     for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
      history.setdefault((league,team),[]).append({
       "date":kickoff.isoformat(),"gf":gf,"ga":ga,
       "result":"W" if gf>ga else "D" if gf==ga else "L",
       "opponent":opponent,"home":home,"away":away,
       "homeGoals":hg,"awayGoals":ag,"teamIsHome":team==home,
       "historySource":"SportScore"
      })
     sportscore_history_counts[league]+=1
     continue
    if is_cancelled or kickoff<=now or kickoff>now+datetime.timedelta(days=WINDOW_DAYS): continue
    all_matches.append({
     "id":"sportscore-"+str(row.get("id") or (league+"-"+kickoff.isoformat()+"-"+home+"-"+away)),
     "date":kickoff.isoformat(),"timestamp":kickoff.timestamp(),"league":league,
     "home":home,"away":away,"venue":row.get("venue"),"status":"scheduled",
     "analysisStatus":"fixture_only","prediction":{},"h2h":[],
     "source":"SportScore API","dataCoverage":{"fixture":"SportScore API"}
    })
    sportscore_fixture_counts[league]+=1
 errors.extend(["SportScore "+msg for msg in sportscore_errors[:25]])
 _diag("SportScore","error" if sportscore_errors else "ok" if sum(sportscore_fixture_counts.values()) else "empty",
       sportscore_http_success==len(sportscore_days),sum(sportscore_fixture_counts.values()),
       "; ".join(sportscore_errors[:8]) or None,dailyRequests=len(sportscore_days),
       httpSuccesses=sportscore_http_success,emptyResponses=sportscore_http_empty,
       failedDays=len(sportscore_errors),perLeague=dict(sorted(sportscore_fixture_counts.items())),
       historyByLeague=dict(sorted(sportscore_history_counts.items())))
 print("SportScore upcoming fixtures by league:",json.dumps(dict(sorted(sportscore_fixture_counts.items())),ensure_ascii=False))
 print("SportScore completed history matches by league:",json.dumps(dict(sorted(sportscore_history_counts.items())),ensure_ascii=False))
 print("SportScore daily requests:",len(sportscore_days),"; HTTP success:",sportscore_http_success,"; empty responses:",sportscore_http_empty,"; errors:",len(sportscore_errors))
 # Optional registered-token source: football-data.org (independent of ESPN/API-Football).
 # Its free tier covers only selected competitions; omit competitions without a documented code.
 football_data_counts=collections.Counter()
 football_data_history_counts=collections.Counter()
 if FOOTBALL_DATA_TOKEN:
  for comp in COMPETITIONS:
   code=comp.get("footballDataCode")
   if not code: continue
   league=comp["name"]
   params=urllib.parse.urlencode({
    "dateFrom":(now.date()-datetime.timedelta(days=45)).isoformat(),
    "dateTo":(now.date()+datetime.timedelta(days=WINDOW_DAYS)).isoformat()
   })
   url="https://api.football-data.org/v4/competitions/"+urllib.parse.quote(str(code),safe="")+"/matches?"+params
   req=urllib.request.Request(url,headers={"X-Auth-Token":FOOTBALL_DATA_TOKEN,"Accept":"application/json","User-Agent":"RamoBet/1.0"})
   try:
    with urllib.request.urlopen(req,timeout=15) as resp: payload=json.load(resp)
    rows=payload.get("matches",[]) if isinstance(payload,dict) else []
    for row in rows:
     if not isinstance(row,dict): continue
     rawdate=row.get("utcDate")
     home=(row.get("homeTeam") or {}).get("name")
     away=(row.get("awayTeam") or {}).get("name")
     if not rawdate or not home or not away: continue
     try:
      kickoff=datetime.datetime.fromisoformat(str(rawdate).replace("Z","+00:00"))
      if kickoff.tzinfo is None: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
      kickoff=kickoff.astimezone(datetime.timezone.utc)
     except Exception: continue
     status=str(row.get("status") or "").upper()
     if status=="FINISHED":
      hg,ag=full_time_score(row.get("score") or {})
      if hg is None or ag is None: continue
      try: hg=int(hg); ag=int(ag)
      except (TypeError,ValueError): continue
      if hg<0 or ag<0: continue
      for team,gf,ga,opponent in ((home,hg,ag,away),(away,ag,hg,home)):
       history.setdefault((league,team),[]).append({
        "date":kickoff.isoformat(),"gf":gf,"ga":ga,
        "result":"W" if gf>ga else "D" if gf==ga else "L",
        "opponent":opponent,"home":home,"away":away,
        "homeGoals":hg,"awayGoals":ag,"teamIsHome":team==home,
        "historySource":"football-data.org"
       })
      football_data_history_counts[league]+=1
     elif now<kickoff<=now+datetime.timedelta(days=WINDOW_DAYS) and status not in {"CANCELLED","POSTPONED","SUSPENDED","ABANDONED"}:
      all_matches.append({
       "id":"football-data-"+str(row.get("id") or (code+"-"+kickoff.isoformat()+"-"+home+"-"+away)),
       "date":kickoff.isoformat(),"timestamp":kickoff.timestamp(),"league":league,
       "home":home,"away":away,"venue":row.get("venue"),"status":"scheduled",
       "analysisStatus":"fixture_only","prediction":{},"h2h":[],
       "source":"football-data.org","dataCoverage":{"fixture":"football-data.org"}
      })
      football_data_counts[league]+=1
   except Exception as exc:
    errors.append(league+" football-data.org: "+str(exc)[:120])
 print("football-data.org upcoming fixtures by league:",json.dumps(dict(sorted(football_data_counts.items())),ensure_ascii=False))
 _diag("football-data.org","error" if any("football-data.org:" in e for e in errors) else "ok" if sum(football_data_counts.values()) else "empty",
       not any("football-data.org:" in e for e in errors),sum(football_data_counts.values()),
       "; ".join([e for e in errors if "football-data.org:" in e][:8]) or None,
       perLeague=dict(sorted(football_data_counts.items())),historyByLeague=dict(sorted(football_data_history_counts.items())))
 print("football-data.org upcoming fixtures by league:",json.dumps(dict(sorted(football_data_counts.items())),ensure_ascii=False))
 print("football-data.org completed-history matches by league:",json.dumps(dict(sorted(football_data_history_counts.items())),ensure_ascii=False))
 # Canonical team names before source merging; this removes ESPN/OpenFootball alias duplicates.
 import unicodedata,math
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
def normalize_team_name(value):
 key=re.sub(r"[^a-z0-9]","",unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode().lower())
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
 return aliases.get(key,key)
def local_day(value):
 dt=datetime.datetime.fromisoformat(str(value).replace("Z","+00:00"))
 if dt.tzinfo is None: dt=dt.replace(tzinfo=datetime.timezone.utc)
 from zoneinfo import ZoneInfo
 return dt.astimezone(ZoneInfo(CONFIG.get("timezone","Europe/Berlin"))).date().isoformat()
def fixture_signature(league,date_value,home,away):
 return (str(league or ""),local_day(date_value),tuple(sorted((normalize_team_name(home),normalize_team_name(away)))))
def fixture_signature_for_item(item):
 return fixture_signature(item.get("league"),item.get("date"),item.get("home"),item.get("away"))
def cache_is_fresh(timestamp, kickoff, near_hours=36):
 if not timestamp: return False
 try:
  updated=datetime.datetime.fromisoformat(str(timestamp).replace("Z","+00:00"))
  if updated.tzinfo is None: updated=updated.replace(tzinfo=datetime.timezone.utc)
  ttl_hours=3 if kickoff and kickoff-now<=datetime.timedelta(hours=near_hours) else 12
  return datetime.timedelta(0)<=now-updated<=datetime.timedelta(hours=ttl_hours)
 except Exception:
  return False
def coverage_summary(fixtures):
 by_league=collections.Counter(x.get("league","UNKNOWN") for x in fixtures)
 by_source=collections.Counter(x.get("source","unknown") for x in fixtures)
 by_analysis=collections.defaultdict(collections.Counter)
 for item in fixtures:
  by_analysis[item.get("league","UNKNOWN")][item.get("analysisStatus","unknown")]+=1
 return {
  "fixtureCountsByLeague":dict(sorted(by_league.items())),
  "fixtureCountsBySource":dict(sorted(by_source.items())),
  "analysisStatusByLeague":{k:dict(v) for k,v in sorted(by_analysis.items())},
  "totalFixtures":len(fixtures)
}
try:
 previous_data=json.loads(OUT.read_text(encoding="utf-8"))
 if not isinstance(previous_data,dict): previous_data={}
except Exception:
 previous_data={}
# Reuse team-level research between scheduled refreshes to reduce duplicate source traffic.
def hydrate_team_research_cache(data):
 cache=data.get("teamResearchCache") if isinstance(data.get("teamResearchCache"),dict) else {}
 for kind in ("news","googleSearch"):
  stored=cache.get(kind) if isinstance(cache.get(kind),dict) else {}
  for team,entry in stored.items():
   if isinstance(team,str) and isinstance(entry,dict) and isinstance(entry.get("items"),list):
    TEAM_RESEARCH_CACHE[kind][team]=entry
 if any(TEAM_RESEARCH_CACHE[kind] for kind in ("news","googleSearch")):
  return
 stamp=data.get("generatedAt")
 if not stamp: return
 legacy_fixtures=data.get("recommendations") if isinstance(data.get("recommendations"),list) else []
 for old in legacy_fixtures:
  if not isinstance(old,dict): continue
  coverage=old.get("dataCoverage") or {}
  home=old.get("home"); away=old.get("away")
  news=old.get("teamNews") if isinstance(old.get("teamNews"),dict) else {}
  if "nicht abgefragt" not in str(coverage.get("teamNews","")).lower() and "budget" not in str(coverage.get("teamNews","")).lower():
   for team,side in ((home,"home"),(away,"away")):
    if team and isinstance(news.get(side),list):
     TEAM_RESEARCH_CACHE["news"].setdefault(team,{"items":news[side],"updatedAt":stamp})
  google=old.get("googleResearch") if isinstance(old.get("googleResearch"),dict) else {}
  if "nur ein teil" not in str(coverage.get("googleResearch","")).lower():
   for team,side in ((home,"home"),(away,"away")):
    if team and isinstance(google.get(side),list):
     TEAM_RESEARCH_CACHE["googleSearch"].setdefault(team,{"items":google[side],"updatedAt":stamp})
hydrate_team_research_cache(previous_data)
previous_api={}
for old in previous_data.get("recommendations",[]) if isinstance(previous_data.get("recommendations"),list) else []:
 if not isinstance(old,dict): continue
 try: sig=fixture_signature_for_item(old)
 except Exception: continue
 previous_api[sig]={
  "fixtureId":old.get("apiFootballFixtureId"),
  "prediction":old.get("apiFootballPrediction"),
  "predictionUpdatedAt":old.get("apiFootballPredictionUpdatedAt"),
  "injuries":old.get("apiFootballInjuries"),
  "injuriesUpdatedAt":old.get("apiFootballInjuriesUpdatedAt"),
  "lineups":old.get("apiFootballLineups"),
  "lineupsUpdatedAt":old.get("apiFootballLineupsUpdatedAt")
 }
fixtures,errors,team_history=fixture_fallback()
for item in fixtures:
 try: cached=previous_api.get(fixture_signature_for_item(item))
 except Exception: cached=None
 if not cached: continue
 if cached.get("fixtureId"): item["apiFootballFixtureId"]=cached["fixtureId"]
 try:
  kickoff=datetime.datetime.fromisoformat(str(item["date"]).replace("Z","+00:00"))
  if kickoff.tzinfo is None: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
 except Exception: kickoff=None
 if cached.get("predictionUpdatedAt") and cache_is_fresh(cached.get("predictionUpdatedAt"),kickoff):
  item["apiFootballPrediction"]=cached.get("prediction")
  item["apiFootballPredictionUpdatedAt"]=cached["predictionUpdatedAt"]
  item.setdefault("dataCoverage",{})["apiFootballPrediction"]=(
   "API-Football-Prognose (zwischengespeichert)" if cached.get("prediction") else "Letzte API-Abfrage lieferte keine Prognose"
  )
 if cached.get("injuries") is not None and cache_is_fresh(cached.get("injuriesUpdatedAt"),kickoff):
  item["apiFootballInjuries"]=cached["injuries"]
  item["apiFootballInjuriesUpdatedAt"]=cached["injuriesUpdatedAt"]
  item.setdefault("dataCoverage",{})["injuries"]="API-Football (zwischengespeichert)"
 if cached.get("lineups") is not None and cache_is_fresh(cached.get("lineupsUpdatedAt"),kickoff):
  item["apiFootballLineups"]=cached["lineups"]
  item["apiFootballLineupsUpdatedAt"]=cached["lineupsUpdatedAt"]
  item.setdefault("dataCoverage",{})["lineups"]="API-Football (zwischengespeichert)"
if not KEY:
 _diag("API-Football","not_configured",None,0,"API_FOOTBALL_KEY is empty")
 coverage=coverage_summary(fixtures)
 save({
  "generatedAt":now.isoformat(),"status":"free_mode",
  "message":"Kostenloser Modus: alle zehn konfigurierten Wettbewerbe, 14-Tage-Spielplan, historische Ergebnisse aus OpenFootball/ESPN, heim-/auswärtsgewichtete Poisson-Schätzung sowie Team-News und Google-Recherche mit begrenztem Abrufbudget. API-Football-Detaildaten benötigen einen konfigurierten Schlüssel.",
  "configuredLeagues":[x["name"] for x in COMPETITIONS],"fixtureWindowDays":WINDOW_DAYS,
  "teamHistory":team_history,"coverage":coverage,"apiFootball":{"configured":False,"requestsUsed":0},"sourceDiagnostics":SOURCE_DIAGNOSTICS,
  "recommendations":fixtures,"fixtures":fixtures,"errors":errors[:50],
  "source":"OpenFootball + ESPN + OpenLigaDB + Google News RSS + Google Suche + Poisson-Basismodell"
 })
 print("Free mode; fixtures=%s model_estimates=%s limited=%s errors=%s"%(len(fixtures),sum(x.get("analysisStatus")=="model_estimate" for x in fixtures),sum(x.get("analysisStatus")=="limited_model" for x in fixtures),len(errors)))
 print("Coverage:",json.dumps(coverage,ensure_ascii=False))
 raise SystemExit(0)
# A configured API key enriches the complete free-source baseline rather than replacing it.
api_status=None
try:
 api_status=api("status",{})
except Exception as exc:
 errors.append("API-Football status: "+str(exc)[:120])
 print("API-Football unavailable; retaining complete free-source baseline:",str(exc)[:160])
api_fixtures_matched=0; api_only_fixtures=0; predictions_added=0; injuries_added=0
if api_status is not None:
 season=now.year if now.month>=7 else now.year-1
 start=now.date().isoformat()
 end=(now.date()+datetime.timedelta(days=WINDOW_DAYS)).isoformat()
 base_index={}
 for item in fixtures:
  try: base_index.setdefault(fixture_signature_for_item(item),[]).append(item)
  except Exception: pass
 api_rows=[]; api_fixtures_matched=0; api_only_fixtures=0
 for comp in COMPETITIONS:
  if used>=BUDGET or (API_DAILY_REMAINING is not None and API_DAILY_REMAINING<=DAILY_QUOTA_RESERVE): break
  league_name=comp["name"]; league_id=comp.get("apiFootballId")
  if league_id is None: continue
  try:
   result=api("fixtures",{"league":league_id,"season":season,"from":start,"to":end,"timezone":CONFIG.get("timezone","Europe/Berlin")})
  except Exception as exc:
   text=str(exc)
   if "Run-Budget erreicht" in text or "Tagesquote geschützt" in text:
    errors.append("API-Football fixture enrichment paused: "+text); break
   errors.append(league_name+" API-Football fixtures: "+text[:100]); continue
  for row in result if isinstance(result,list) else []:
   fixture=row.get("fixture") or {}; teams=row.get("teams") or {}
   rawdate=fixture.get("date"); home=(teams.get("home") or {}).get("name"); away=(teams.get("away") or {}).get("name")
   if not rawdate or not home or not away: continue
   try:
    kickoff=datetime.datetime.fromisoformat(str(rawdate).replace("Z","+00:00"))
    if kickoff.tzinfo is None: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
   except Exception: continue
   if kickoff<=now or kickoff>now+datetime.timedelta(days=WINDOW_DAYS): continue
   try: sig=fixture_signature(league_name,rawdate,home,away)
   except Exception: continue
   venue=(fixture.get("venue") or {}).get("name")
   api_match={"league":league_name,"date":kickoff.isoformat(),"home":home,"away":away,"fixtureId":fixture.get("id"),"venue":venue,
              "homeId":(teams.get("home") or {}).get("id"),"awayId":(teams.get("away") or {}).get("id"),"timestamp":kickoff.timestamp()}
   matches=base_index.get(sig,[])
   if matches:
    item=matches[0]
    item["apiFootballFixtureId"]=fixture.get("id")
    item["apiFootballHomeId"]=api_match["homeId"]; item["apiFootballAwayId"]=api_match["awayId"]
    if venue and not item.get("venue"): item["venue"]=venue
    api_fixtures_matched+=1
   else:
    model=poisson_match_model([],[])
    winner_key=max(model["probabilities"],key=model["probabilities"].get)
    model["winner"]={"home":home,"draw":"Unentschieden","away":away}[winner_key]
    model["winnerComment"]="Eingeschränkte Basis-Modellschätzung; keine passenden jüngsten Ergebnisse gefunden"
    item={"id":"api-football-"+str(fixture.get("id")),"date":kickoff.isoformat(),"timestamp":kickoff.timestamp(),
          "league":league_name,"home":home,"away":away,"venue":venue,"status":"scheduled","source":"API-Football",
          "analysisStatus":"limited_model","prediction":model,"form":{"home":[],"away":[]},"h2h":[],
          "dataCoverage":{"fixture":"API-Football","recentForm":"Keine passenden jüngsten Team-Ergebnisse in den kostenlosen Quellen",
                          "prediction":"Eingeschränkte Poisson-Basisschätzung; Teamdaten fehlen"}}
    fixtures.append(item); base_index.setdefault(sig,[]).append(item); api_only_fixtures+=1
   item["apiFootballFixtureId"]=fixture.get("id")
   if item.get("apiFootballPredictionUpdatedAt") and cache_is_fresh(item.get("apiFootballPredictionUpdatedAt"),kickoff):
    continue
   api_rows.append((kickoff.timestamp(),item,api_match))
 # Use a bounded number of individual prediction calls; all remaining fixtures retain the tested baseline model.
 api_rows.sort(key=lambda x:x[0])
 predictions_added=0
 for _,item,api_match in api_rows[:5]:
  if used>=BUDGET or (API_DAILY_REMAINING is not None and API_DAILY_REMAINING<=DAILY_QUOTA_RESERVE): break
  try:
   rows=api("predictions",{"fixture":api_match["fixtureId"]})
   item["apiFootballPredictionUpdatedAt"]=now.isoformat()
   if rows:
    row=rows[0]; pred=row.get("predictions") or {}; percent=pred.get("percent") or {}; goals=pred.get("goals") or {}
    item["apiFootballPrediction"]={
     "winner":(pred.get("winner") or {}).get("name"),
     "winnerComment":(pred.get("winner") or {}).get("comment"),
     "advice":pred.get("advice"),"underOver":pred.get("under_over"),
     "homeGoals":goals.get("home"),"awayGoals":goals.get("away"),"confidence":pred.get("confidence"),
     "probabilities":{"home":pct(percent.get("home")),"draw":pct(percent.get("draw")),"away":pct(percent.get("away"))},
     "comparison":row.get("comparison") or {}
    }
    item.setdefault("dataCoverage",{})["apiFootballPrediction"]="API-Football offizielle Pre-Match-Prognose"
    predictions_added+=1
   else:
    item["apiFootballPrediction"]=None
    item.setdefault("dataCoverage",{})["apiFootballPrediction"]="Abfrage erfolgt; keine Prognose von API-Football geliefert"
  except Exception as exc:
   errors.append("API-Football prediction "+str(api_match["fixtureId"])+": "+str(exc)[:100])
   if "Run-Budget erreicht" in str(exc) or "Tagesquote geschützt" in str(exc): break
 # Injury data is reserved for the next three matches with API IDs and kickoff within 72 hours.
 injuries_added=0
 injury_candidates=[]
 for item in fixtures:
  fid=item.get("apiFootballFixtureId")
  if not fid: continue
  try: kickoff=datetime.datetime.fromisoformat(str(item["date"]).replace("Z","+00:00"))
  except Exception: continue
  if kickoff.tzinfo is None: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
  if now<kickoff<=now+datetime.timedelta(hours=72) and not cache_is_fresh(item.get("apiFootballInjuriesUpdatedAt"),kickoff,near_hours=48):
   injury_candidates.append((kickoff.timestamp(),item))
 for _,item in sorted(injury_candidates,key=lambda x:x[0])[:3]:
  if used>=BUDGET or (API_DAILY_REMAINING is not None and API_DAILY_REMAINING<=DAILY_QUOTA_RESERVE): break
  try:
   rows=api("injuries",{"fixture":item["apiFootballFixtureId"]})
   item["apiFootballInjuries"]=[{"player":(x.get("player") or {}).get("name"),"type":(x.get("player") or {}).get("type"),
     "reason":(x.get("player") or {}).get("reason"),"team":(x.get("team") or {}).get("name")} for x in rows]
   item["apiFootballInjuriesUpdatedAt"]=now.isoformat()
   item.setdefault("dataCoverage",{})["injuries"]="API-Football" if rows else "Abfrage erfolgt; keine Einträge geliefert"
   injuries_added+=bool(rows)
  except Exception as exc:
   errors.append("API-Football injuries "+str(item["apiFootballFixtureId"])+": "+str(exc)[:100])
   if "Run-Budget erreicht" in str(exc) or "Tagesquote geschützt" in str(exc): break
 # Ask for a lineup only when kickoff is near enough for official lineups to exist; at most one extra call per run.
 lineup_candidates=[]
 for item in fixtures:
  fid=item.get("apiFootballFixtureId")
  if not fid: continue
  try: kickoff=datetime.datetime.fromisoformat(str(item["date"]).replace("Z","+00:00"))
  except Exception: continue
  if kickoff.tzinfo is None: kickoff=kickoff.replace(tzinfo=datetime.timezone.utc)
  if now<kickoff<=now+datetime.timedelta(hours=36) and not cache_is_fresh(item.get("apiFootballLineupsUpdatedAt"),kickoff,near_hours=36):
   lineup_candidates.append((kickoff.timestamp(),item))
 for _,item in sorted(lineup_candidates,key=lambda x:x[0])[:1]:
  if used>=BUDGET or (API_DAILY_REMAINING is not None and API_DAILY_REMAINING<=DAILY_QUOTA_RESERVE): break
  try:
   rows=api("fixtures/lineups",{"fixture":item["apiFootballFixtureId"]})
   item["apiFootballLineups"]=[{"team":x.get("team",{}).get("name"),"formation":x.get("formation"),
    "startXI":[(z.get("player") or {}).get("name") for z in x.get("startXI",[])],
    "substitutes":[(z.get("player") or {}).get("name") for z in x.get("substitutes",[])]} for x in rows]
   item["apiFootballLineupsUpdatedAt"]=now.isoformat()
   item.setdefault("dataCoverage",{})["lineups"]="API-Football" if rows else "Abfrage erfolgt; offizielle Aufstellungen noch nicht veröffentlicht"
  except Exception as exc:
   errors.append("API-Football lineups "+str(item["apiFootballFixtureId"])+": "+str(exc)[:100])
# Preserve model predictions for every fixture and attach a compact quality summary.
for item in fixtures:
 item.setdefault("dataCoverage",{})
 if item.get("apiFootballFixtureId") and "apiFootballPrediction" not in item:
  item["dataCoverage"]["apiFootballPrediction"]="API-Football-Fixture zugeordnet; Prognose aus Tagesbudget noch nicht abgefragt"
 if item.get("apiFootballFixtureId") and "injuries" not in item.get("dataCoverage",{}):
  item["dataCoverage"].setdefault("injuries","Nicht für dieses Spiel im begrenzten API-Budget abgefragt")
coverage=coverage_summary(fixtures)
api_meta={
 "configured":True,"requestsUsed":used,"perRunBudget":BUDGET,
 "dailyRequestsRemaining":API_DAILY_REMAINING,
 "fixturesMatched":api_fixtures_matched,"apiOnlyFixturesAdded":api_only_fixtures,
 "officialPredictionsAdded":predictions_added,"fixturesWithInjuryRows":injuries_added
}
save({
 "generatedAt":now.isoformat(),"status":"hybrid_mode",
 "message":"Alle Spiele aus dem festen 10-Ligen-Spielplan werden mit dem Basis-Modell analysiert. API-Football erweitert ausgewählte zeitnahe Spiele quota-bewusst; fehlende Detaildaten bleiben ausdrücklich gekennzeichnet.",
 "configuredLeagues":[x["name"] for x in COMPETITIONS],"fixtureWindowDays":WINDOW_DAYS,
 "teamHistory":team_history,"coverage":coverage,"apiFootball":api_meta,"sourceDiagnostics":SOURCE_DIAGNOSTICS,
 "recommendations":fixtures,"fixtures":fixtures,"errors":errors[:50],
 "source":"OpenFootball + ESPN + OpenLigaDB + API-Football (quota-aware) + Google News RSS + Google Suche + Poisson-Modell"
})
print("Hybrid mode; fixtures=%s model_estimates=%s limited=%s api_matched=%s api_only=%s official_predictions=%s requests=%s daily_remaining=%s errors=%s" % (
 len(fixtures),sum(x.get("analysisStatus")=="model_estimate" for x in fixtures),
 sum(x.get("analysisStatus")=="limited_model" for x in fixtures),api_fixtures_matched,api_only_fixtures,
 predictions_added,used,API_DAILY_REMAINING,len(errors)))
print("Coverage:",json.dumps(coverage,ensure_ascii=False))

