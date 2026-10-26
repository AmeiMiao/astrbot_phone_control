"""按已保存连接方式标识原生表单适用项，字段与参数保持完整。"""

def populate_connection_options(config):
    schema=getattr(config,'schema',None)
    if not isinstance(schema,dict):return
    adb=config.get('adb',{})
    app=adb.get('mode') in ('app','使用 App 控制')
    video=adb.get('app_screen_source','scrcpy 视频取帧') in ('scrcpy','scrcpy 视频取帧')
    provider=config.get('traffic',{}).get('provider','mitmproxy')
    def field(path):
        current=schema
        parts=path.split('.')
        for index,key in enumerate(parts):
            current=current.get(key)
            if not isinstance(current,dict):return None
            if index<len(parts)-1:current=current.get('items',{})
        return current
    def hide(path,hidden):
        spec=field(path)
        if spec is not None:spec['invisible']=bool(hidden)
    for name in ('app_frame_max_size','app_frame_fps','app_frame_quality','app_frame_max_age_ms'):
        hide('adb.'+name,not (app and video))
    hide('control',app)
    hide('adb.connect_timeout',app)
    hide('perception.screen_source',app)
    hide('perception.screenshot_mode',app and video)
    hide('traffic.transport',app)
    hide('traffic.mitmdump_path',app and provider in ('app','App 抓包'))
    for group in ('wireguard','tailscale','zerotier'):
        for name in ('auto_adb','adb_port','local_adb_port','adb_local_port','adb_target_port','proxy_port'):
            hide('network.'+group+'.'+name,app)
    hide('network.tailscale.phone_ip',app)
