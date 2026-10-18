import pytest
from web_admin import effective_config,save_settings

def test_mode_locked():
    assert effective_config()['mode']=='PAPER_ONLY'

def test_reject_invalid_risk():
    with pytest.raises(ValueError):save_settings({'risk_per_trade_pct':10})

def test_reject_unknown_setting():
    with pytest.raises(ValueError):save_settings({'mode':'LIVE'})
