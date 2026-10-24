"""三种自主模式互斥、导入前迁移和命令补丁。"""

import json
from pathlib import Path

import pytest

from core.autonomy_config import migrate_autonomy, migrate_autonomy_file, normalize_autonomy_patch
from core.config import PhoneConfig
from core.play_window import activity_allowed
from core.identity import PLUGIN_NAME


@pytest.mark.parametrize('mode,flags', [('随机玩',(True,False,False)),('自动刷',(False,True,False)),('定时玩',(False,False,True))])
def test_explicit_mode_has_one_active_loop(mode,flags):
    auto=PhoneConfig.from_mapping({'auto':{'mode':mode,'activity_enabled':True,'random_enabled':True,'continuous_enabled':True,'enabled':True}}).auto
    assert (auto.random_enabled,auto.continuous_enabled,auto.enabled)==flags
    disabled=PhoneConfig.from_mapping({'auto':{'mode':mode,'activity_enabled':False}}).auto
    for origin in ('phone_random_play','phone_continuous',PLUGIN_NAME):
        assert not activity_allowed(disabled,origin)
    assert activity_allowed(disabled,'phone_play')


@pytest.mark.parametrize('initial,mode,active', [({},'随机玩',True),({'random_enabled':False,'continuous_enabled':False,'enabled':False},'随机玩',False),({'continuous_enabled':True,'enabled':True},'自动刷',True),({'enabled':True},'定时玩',True),({'mode':'随机玩','activity_enabled':False,'continuous_enabled':True},'随机玩',False)])
def test_migration_preserves_choice_and_parameters(initial,mode,active):
    raw={'auto':dict(initial, random_windows=['08:00-10:00'],continuous_windows=['19:00-22:00'])}
    assert migrate_autonomy(raw)
    assert raw['auto']['mode']==mode and raw['auto']['activity_enabled']==active
    assert raw['auto']['random_windows']==['08:00-10:00']
    assert raw['auto']['continuous_windows']==['19:00-22:00']
    assert not any(k in raw['auto'] for k in ('enabled','random_enabled','continuous_enabled'))
    assert not migrate_autonomy(raw)


def test_file_backup_preserves_original_bytes_and_is_one_time(tmp_path):
    path=tmp_path/'plugin_config.json';original=b'\xef\xbb\xbf'+json.dumps({'auto':{'continuous_enabled':True,'continuous_windows':['22:00-02:00']}}).encode();path.write_bytes(original)
    assert migrate_autonomy_file(path)
    backup=path.with_name(path.name+'.autonomy-backup')
    assert backup.read_bytes()==original
    assert json.loads(path.read_text())['auto']['mode']=='自动刷'
    assert not migrate_autonomy_file(path)
    assert backup.read_bytes()==original


def test_invalid_mode_rejected():
    with pytest.raises(ValueError,match='auto.mode'):
        PhoneConfig.from_mapping({'auto':{'mode':'bad'}})


def test_partial_patch_switches_explicitly_and_keeps_nonselected_preferences():
    current={'auto':{'mode':'定时玩','activity_enabled':True}}
    assert normalize_autonomy_patch(current,{'auto':{'continuous_enabled':True,'continuous_gap_seconds':5}})=={'auto':{'mode':'自动刷','activity_enabled':True,'continuous_gap_seconds':5}}
    assert normalize_autonomy_patch(current,{'auto':{'continuous_enabled':False}})=={'auto':{'mode':'定时玩','activity_enabled':True}}
    assert normalize_autonomy_patch(current,{'auto':{'enabled':False}})=={'auto':{'mode':'定时玩','activity_enabled':False}}


def test_schema_mode_fields_have_local_conditions():
    schema=json.loads((Path(__file__).resolve().parents[1]/'_conf_schema.json').read_text())['auto']['items']
    assert schema['mode']['options']==['随机玩','自动刷','定时玩']
    for key in ('enabled','random_enabled','continuous_enabled'):
        assert key not in schema
    for key,mode in [('cron','定时玩'),('apps','定时玩'),('session','定时玩'),('random_windows','随机玩'),('random_interval_min','随机玩'),('continuous_windows','自动刷'),('continuous_apps','自动刷')]:
        assert schema[key]['condition']=={'mode':mode}
