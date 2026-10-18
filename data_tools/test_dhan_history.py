from datetime import date
from dhan_history import body, chunks, flatten

def test_chunks():
    c=list(chunks(date(2026,1,1),date(2026,3,1)))
    assert c[0] == (date(2026,1,1),date(2026,1,30))
    assert c[-1][1] == date(2026,3,1)

def test_payload():
    p=body('BANKNIFTY',date(2026,1,1),date(2026,1,2),'ATM+1','PUT',5,'MONTH',1)
    assert p['securityId']==25 and p['drvOptionType']=='PUT'

def test_flatten():
    d={'timestamp':[1], **{k:[10] for k in ['open','high','low','close','iv','volume','strike','oi','spot']}}
    rows=flatten({'data':{'ce':d}},'CALL',{'index':'NIFTY'})
    assert rows[0]['timestamp']==1 and rows[0]['iv']==10
