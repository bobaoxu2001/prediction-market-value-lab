# DESIGN-TIME RECORD, not reproducible as committed: it read the session scratchpad (the
# saved program history lip_history.json, lip.json, a local market-metadata database and a
# classifier module). It is kept so the section 2.5 numbers in docs/lip-preregistration.md
# can be traced. The study's own admission code is pmvl_markets.lip.universe.

import json, re, hashlib, sqlite3, statistics as st, sys
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
SP=sys.argv[1]
sys.path.insert(0, SP+'lipstudy')
from classify2 import classify
rows={}
for r in json.load(open(SP+'lip_history.json'))+json.load(open(SP+'lip.json')):
    rows[r['id']]=r
P=lambda s: datetime.fromisoformat(s.replace('Z','+00:00'))
INPLAY = r"^KX(TT[A-Z]*MATCH|MIXEDDOUBLESMATCH|SQUASHMATCH|DARTSMATCH|CHESSMATCH|CHESSGAME|WRESTLINGMATCH|CRICKET[A-Z0-9]*MATCH|VALORANTGAME|LOLGAMES|CBAGAME|NCAA[A-Z]*GAME|MLBFTGAME|CLUBFGAME|EFLCUPGAME|BALLERLEAGUEGAME|PICKLEBALLGAMES|PPLGAMES)$"
FAST = [
 ("X1_intraday_15m", r"15M$"),
 ("X1_hourly_weather", r"^KXTEMP[A-Z]*H$"),
 ("X2_inplay", INPLAY),
 ("X2_live_event", r"MENTION|SAY$|^KXTRUTHSOCIAL|(GAME|GAMES|MATCH|SPREAD|TOTAL)$"),
 ("X3_realtime_obs", r"^KX(HIGH|LOW|TEMP|RAIN|SNOW|WIND)|ERCOT|PJM"),
 ("X4_continuous_underlying", r"^KX(BTC|ETH|SOL|XRP|DOGE|CRYPTO|EURUSD|USDJPY|GBPUSD|INX|NASDAQ|WTI|BRENT|GOLD|SILVER|NATGAS|COPPER|PLAT|PALL)"),
]
def fastcls(ser):
    for n,pat in FAST:
        if re.search(pat, ser):
            if n=="X2_live_event" and "SEASON" in ser and re.search(r"(GAME|GAMES|MATCH|SPREAD|TOTAL)$",ser) and not re.search(r"MENTION|SAY$|^KXTRUTHSOCIAL",ser):
                continue
            return n
    return None
progs=[]
for r in rows.values():
    if r.get('incentive_type')!='liquidity': continue
    s,e=P(r['start_date']),P(r['end_date'])
    if e<=s: continue
    hrs=(e-s).total_seconds()/3600
    t=r['market_ticker']; ser=t.split('-')[0]
    progs.append(dict(id=r['id'],t=t,ser=ser,cls=classify(ser),fc=fastcls(ser),s=s,e=e,hrs=hrs,R=r['period_reward']/1e4,rate=r['period_reward']/1e4/hrs,T=float(r['target_size_fp'])))
STATES=set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
allser={p['ser'] for p in progs}
def fam(s):
    if len(s)>4 and s[-2:] in STATES and s[:-2] in allser: return s[:-2]
    return s
def hk(p): return hashlib.sha256(('pmvl-lip-1.0.0|'+p['t']+'|'+p['id']).encode()).hexdigest()
end=datetime(2026,9,24,2,50,tzinfo=timezone.utc); start=end-timedelta(days=7)
ts=[start+timedelta(minutes=10*i) for i in range(7*24*6)]
def inwin(t):
    m=t.hour*60+t.minute
    return m>=12*60+30 or m<45
base=[p for p in progs if p['hrs']>=6 and p['rate']>=2]
elig=[p for p in base if p['fc'] is None]
# excluded share by $ among long >=2/h
exc=Counter(); tot=0
for t in ts:
    for p in base:
        if p['s']<=t<p['e']:
            tot+=p['rate']; exc[p['fc']]+=p['rate']
print('long>=6h,>=2/h $ share by exclusion class', {k:round(v/tot,3) for k,v in exc.items()})
allrate=0; eligrate=0; winelig=0
for t in ts:
    for p in progs:
        if p['s']<=t<p['e']:
            allrate+=p['rate']
            if p['hrs']>=6 and p['rate']>=2 and p['fc'] is None:
                eligrate+=p['rate']
                if inwin(t): winelig+=p['rate']
print('eligible share of all LIP $', round(eligrate/allrate,3), 'in-window share of eligible $', round(winelig/eligrate,3), 'in-window eligible share of all LIP $', round(winelig/allrate,3))
# admission simulation: admit at first time seen live in window (approx: listed at start), cap 5/family, 200 total
stats=[]
admitted={}
for t in ts:
    if not inwin(t):
        continue
    live=[p for p in elig if p['s']<=t<p['e']]
    # drop admitted no longer live
    admitted={k:v for k,v in admitted.items() if v['s']<=t<v['e']}
    fcount=Counter(fam(v['ser']) for v in admitted.values())
    for p in sorted(live,key=hk):
        if p['id'] in admitted: continue
        f=fam(p['ser'])
        if fcount[f]<5 and len(admitted)<200:
            admitted[p['id']]=p; fcount[f]+=1
    A=list(admitted.values())
    stats.append((len(live),len({fam(p['ser']) for p in live}),len(A),len({fam(p['ser']) for p in A}),len({p['ser'] for p in A}),sum(p['rate'] for p in A),sum(p['rate'] for p in live)))
q=lambda i,a: sorted(s[i] for s in stats)[int(a*(len(stats)-1))]
print('in-window eligible live mkts p10/50/90', q(0,.1),q(0,.5),q(0,.9),' families p10/50/90', q(1,.1),q(1,.5),q(1,.9))
print('admitted mkts p10/50/90/max', q(2,.1),q(2,.5),q(2,.9),max(s[2] for s in stats),' admitted families p10/50/90', q(3,.1),q(3,.5),q(3,.9),' series p50',q(4,.5))
print('admitted pool $/h p10/50/90', round(q(5,.1)),round(q(5,.5)),round(q(5,.9)),' eligible pool $/h p50', round(q(6,.5)))
# family mix of admitted $/h
fm=Counter(); cm=Counter()
for t in ts:
    pass
wk=[p for p in elig if p['e']>start and p['s']<end]
print('week: families', len({fam(p['ser']) for p in wk}), 'series', len({p['ser'] for p in wk}), 'markets', len({p['t'] for p in wk}))
print('eligible duration p10/50/90 h', [round(sorted(p['hrs'] for p in wk)[int(a*(len(wk)-1))],1) for a in (.1,.5,.9)], 'pool p10/50/90', [sorted(p['R'] for p in wk)[int(a*(len(wk)-1))] for a in (.1,.5,.9)])
print('T', Counter(p['T'] for p in wk))
# grid check from db
con=sqlite3.connect('file:/Users/xuao/Projects/prediction-market-value-lab/data/pmvl.db?mode=ro',uri=True)
grid={}
for tk,pls,ts_ in con.execute("select platform_market_id, price_level_structure, tick_size from markets where platform_market_id is not null"):
    grid[tk]=(pls,ts_)
g=Counter(); gd=Counter()
for p in wk:
    k=grid.get(p['t'],('unknown',None))[0] or 'null'
    g[k]+=1; gd[k]+=p['R']
print('grid structure among eligible week markets (count)', g.most_common(), '(pool $)', {k:round(v) for k,v in gd.most_common()})
# admitted market-hours per in-window 10-min step -> per day
mh=sum(s[2] for s in stats)*(10/60)/7
print('admitted market-hours per day (approx)', round(mh))
fam_top=Counter()
