"""Relay组网地址选择、权限边界与原生配置的保留语义。"""
import copy
import json
from pathlib import Path
from core.app_network import relay_route,peer_capabilities,valid_host
from core.app_schema import populate_connection_options


def test_group_address_does_not_silently_fall_back_to_lan():
    waiting=relay_route(port=6190,panel_host='192.0.2.2',network_enabled=True,network={'provider':'zerotier'})
    assert waiting['host']=='' and not waiting['entry_listening']
    ready=relay_route(port=6190,panel_host='192.0.2.2',network_enabled=True,network={'provider':'zerotier','ready':True,'addresses':['10.147.19.75'],'app_port':6190})
    assert ready['host']=='10.147.19.75' and ready['entry_listening']
    explicit=relay_route(port=6190,host='relay.example.com',network_enabled=True,network={})
    assert explicit['host']=='relay.example.com' and explicit['source']=='external'
    for value in ('localhost','127.0.0.1','0.0.0.0','a..b','999.999.999.999','https://relay.test','relay.test@other'):
        assert not valid_host(value)


def test_android_install_version_does_not_imply_audio_capability():
    base={'connected':True,'features':['binary_streams','media_recording','traffic_proxy']}
    old=peer_capabilities(dict(base,sdk_int=28));assert old['files'] and old['record_video'] and not old['playback_audio']
    assert peer_capabilities(base)['playback_audio'] is None
    assert peer_capabilities(dict(base,sdk_int=35))['record_audio']


def test_schema_hides_unused_fields_and_retains_saved_values():
    class Config(dict):pass
    c=Config(adb={'mode':'使用 App 控制','app_screen_source':'scrcpy 视频取帧'},traffic={'provider':'App 抓包'},control={'backend':'scrcpy','scrcpy':{'max_size':1920}},network={'zerotier':{'adb_port':33333}})
    original=copy.deepcopy(dict(c))
    c.schema=json.loads((Path(__file__).parents[1]/'_conf_schema.json').read_text())
    populate_connection_options(c)
    assert c.schema['control']['invisible']
    assert c.schema['traffic']['items']['mitmdump_path']['invisible']
    assert c.schema['network']['items']['zerotier']['items']['adb_port']['invisible']
    assert dict(c)==original
    c['adb']['mode']='直连';c['traffic']['provider']='mitmproxy';populate_connection_options(c)
    assert not c.schema['control']['invisible'] and not c.schema['traffic']['items']['mitmdump_path']['invisible']
    assert c['control']['scrcpy']['max_size']==1920 and c['network']['zerotier']['adb_port']==33333
