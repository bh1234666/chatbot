import copy
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('qq_project_mode', Path(__file__).resolve().parents[1] / 'scripts/qq_project_mode.py')
mode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mode)


def test_switch_preserves_other_transports_and_credentials():
    original = {'network': {'httpServers': [{'port': 5700, 'token': 'private'}],
                            'httpClients': [{'name': 'other', 'enable': True},
                                            {'name': 'chatbot-bridge', 'enable': False, 'url': 'http://localhost:19825/napcat/callback', 'token': 'retained'}],
                            'websocketClients': [{'enable': True}]}, 'extra': {'value': 1}}
    snapshot = copy.deepcopy(original)
    legacy = mode.configure(original, 'legacy')
    assert original == snapshot
    assert legacy['network']['httpClients'][1] == {'name': 'chatbot-bridge', 'enable': True, 'url': 'http://127.0.0.1:8090/napcat/callback', 'token': 'retained'}
    current = mode.configure(legacy, 'current')
    for key in ('httpServers', 'websocketClients'):
        assert current['network'][key] == original['network'][key]
    assert current['network']['httpClients'][0] == original['network']['httpClients'][0]
    assert current['extra'] == original['extra']
    assert not current['network']['httpClients'][1]['enable']
    assert mode.configure(current, 'current') == current


def test_reject_ambiguous_bridge():
    with pytest.raises(RuntimeError, match='Multiple'):
        mode.configure({'network': {'httpClients': [{'name': 'chatbot-bridge'}] * 2}}, 'legacy')


@pytest.mark.parametrize('target', ['legacy', 'current'])
def test_running_opposite_project_blocks_before_config_mutation(target):
    with patch.object(mode, 'listening', return_value=True), patch.object(mode.urllib.request, 'build_opener') as http:
        with pytest.raises(RuntimeError, match='running'):
            mode.switch(target)
        http.assert_not_called()
