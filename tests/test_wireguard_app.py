"""App入口配置与直连转发分别生成，端口和手机身份严格校验。"""
from dataclasses import replace
import pytest
from core.wireguard import WireGuardKeyStore, WireGuardSettings, WireGuardConfigError, build_helper_config


def test_app_ingress_binds_virtual_interface_and_only_configured_phone(tmp_path):
    settings=WireGuardSettings(mode='server',interface_address='10.66.0.1/24',phone_wg_ip='10.66.0.2',adb_local_port=15555,public_endpoint='192.0.2.10:51820')
    keys=WireGuardKeyStore(tmp_path/'keys.json').ensure_all()
    doc=build_helper_config(settings,keys,app_port=6190)
    assert doc['app_port']==6190 and doc['app_peer']=='10.66.0.2'
    assert doc['forwards']==[]
    assert 'app_port' not in build_helper_config(settings,keys)
    for value in (True,-1,80,65536,'6190'):
        with pytest.raises(WireGuardConfigError):build_helper_config(settings,keys,app_port=value)


def test_export_names_actual_app_ingress(tmp_path):
    from core.wireguard import WireGuardManager,WireGuardSettings
    settings=WireGuardSettings(enabled=True,mode='server',interface_address='10.66.0.1/24',phone_wg_ip='10.66.0.2',public_endpoint='relay.example.com')
    manager=WireGuardManager(data_dir=tmp_path,settings_provider=lambda:settings,app_port_provider=lambda:6190)
    value=manager.config_text()
    assert 'Relay App TLS：10.66.0.1:6190' in value
    assert '127.0.0.1:15555' not in value
    assert '[Interface]' in value and '[Peer]' in value
