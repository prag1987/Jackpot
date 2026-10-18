from strategy_lab import analyze,demo

def test_fail_closed():
    assert analyze(demo(),50).direction=='NO_TRADE'
def test_short_data():
    assert analyze(demo().iloc[:10],50,True).direction=='NO_TRADE'
def test_valid_demo():
    result=analyze(demo(),50,True)
    assert result.direction in ('BULL','BEAR','NO_TRADE')
    assert isinstance(result.candidate_names,list)
def test_no_trade_api():
    from engine import DhanReadOnly
    assert '/orders' not in DhanReadOnly._post.__code__.co_consts
