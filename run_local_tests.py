import json, os, sys, urllib.request
from pathlib import Path
BASE=Path(__file__).resolve().parent
URL=os.environ.get('BOT_URL','http://localhost:8080')
def req(method,path,payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    r=urllib.request.Request(URL+path,data=data,headers={'Content-Type':'application/json'},method=method)
    with urllib.request.urlopen(r,timeout=10) as x:return json.loads(x.read().decode())
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))
print('MAGICPIN VERA LOCAL TESTS')
print('-'*55)
assert req('GET','/v1/healthz')['status']=='ok'; print('1. API health                         PASS')
root=BASE/'dataset'/'generated'
D={}
for name,folder,key in [('category','categories','slug'),('merchant','merchants','merchant_id'),('customer','customers','customer_id'),('trigger','triggers','trigger_id')]:
    D[name]={}
    for p in (root/folder).glob('*.json'):
        d=load(p); D[name][str(d.get(key) or d.get('id'))]=d
assert [len(D[x]) for x in ('category','merchant','customer','trigger')]==[5,50,200,100]
print('2. Dataset                           PASS (5/50/200/100)')
for scope in D:
    for oid,payload in D[scope].items():
        r=req('POST','/v1/context',{'scope':scope,'context_id':oid,'version':1,'payload':payload})
        assert r['ok'] and r['updated']
print('3. Context injection                 PASS (355 records)')
h=req('GET','/v1/healthz'); assert (h['categories'],h['merchants'],h['customers'],h['triggers'])==(5,50,200,100); print('4. Context counts                    PASS')
sys.path.insert(0,str(BASE)); import bot
pairs=load(root/'test_pairs.json')['pairs']; assert len(pairs)==30
for pair in pairs:
    m=D['merchant'][pair['merchant_id']]; c=D['category'].get(str(m.get('category_slug'))) or bot.merchant_category(m); t=D['trigger'][pair['trigger_id']]; cust=D['customer'].get(pair['customer_id']) if pair.get('customer_id') else None
    o=bot.compose(c,m,t,cust); assert o['body'] and o['send_as'] in ('vera','merchant_on_behalf') and 'suppression_key' in o and 'rationale' in o
print('5. Canonical 30 cases                PASS')
r=req('POST','/v1/tick',{'now':'2026-09-09T15:00:00','available_triggers':list(D['trigger'].values())}); assert len(r['actions'])<=20 and r['actions']; print(f"6. Tick max 20 actions               PASS ({len(r['actions'])} returned)")
conv=r['actions'][0]['conversation_id']; a=req('POST','/v1/reply',{'conversation_id':conv,'message':'ok let’s do it'}); assert a['action']=='send'; s=req('POST','/v1/reply',{'conversation_id':conv,'message':'STOP'}); assert s['action']=='end'; print('7. Intent + STOP replay              PASS')
r2=req('POST','/v1/tick',{'now':'2026-09-09T15:01:00','available_triggers':list(D['trigger'].values())[20:40]}); assert r2['actions']; conv2=r2['actions'][0]['conversation_id']
for _ in range(3): rr=req('POST','/v1/reply',{'conversation_id':conv2,'message':'Thanks, noted.'})
assert rr['action']=='end'; print('8. Repeated auto-reply protection     PASS')
oid=next(iter(D['merchant'])); stale=req('POST','/v1/context',{'scope':'merchant','context_id':oid,'version':0,'payload':D['merchant'][oid]}); assert stale['updated'] is False; print('9. Stale version protection           PASS')
print('-'*55); print('ALL LOCAL TESTS PASSED')
