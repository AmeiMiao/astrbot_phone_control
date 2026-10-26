"""Relay配对地址与组网就绪状态，网络状态和控制连接分别确认。"""
import ipaddress
import re


def valid_host(value):
    value=str(value or '').strip()
    if not value or len(value)>253:return False
    try:
        address=ipaddress.ip_address(value)
        return not (address.is_loopback or address.is_unspecified or address.is_multicast)
    except ValueError:
        labels=value.split('.')
        return value.lower()!='localhost' and not all(label.isdigit() for label in labels) and all(len(label)<=63 and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?',label) for label in labels)


def relay_route(*,port,host='',panel_host='',network_enabled=False,network=None):
    network=network or {};source='external';message='手机VPN分流需允许Phone Relay；本机ADB授权与插件配对分别完成。'
    if host:
        selected=host.strip()
    elif network_enabled:
        source=network.get('provider','network')
        addresses=network.get('addresses') or []
        selected=next((str(a).split('/')[0] for a in addresses if ':' not in str(a)), '')
        message=network.get('app_error') or network.get('error') or ('组网入口等待手机回连确认。' if selected else '组网节点尚未分配IPv4，请先完成登录或节点授权。')
        if source in ('wireguard','zerotier') and selected and network.get('app_port')!=port:
            message=network.get('app_error') or '组网App入口尚未监听，请检查组件和手机组网IPv4。'
    else:
        selected=panel_host
    if selected and not valid_host(selected):
        selected='';message='请填写手机可访问的插件主机名或组网IP，不能使用localhost、回环或无效地址。'
    return {'host':selected,'port':port,'source':source,'network_ready':bool(network.get('ready')) if network_enabled else None,
            'entry_listening':network.get('app_port')==port if network_enabled and not host else None,'message':message}


def peer_capabilities(peer):
    features=set(peer.get('features') or [])
    sdk=peer.get('sdk_int')
    audio=(sdk>=30) if type(sdk) is int else None
    return {'control':bool(peer.get('connected')),'screen': bool(peer.get('connected')), 'video_frames':'screen_frames' in features,
            'files':'binary_streams' in features,'record_video':'media_recording' in features,
            'playback_audio':audio if 'binary_streams' in features else False,
            'record_audio':audio if 'media_recording' in features else False,
            'traffic':'traffic_proxy' in features,'sdk_int':sdk,
            'adb_uid':peer.get('adb_uid'),'su_present':peer.get('su_present'),
            'endpoint_host':peer.get('endpoint_host'),'endpoint_port':peer.get('endpoint_port'),
            'relay_host':peer.get('relay_host'),'relay_port':peer.get('relay_port'),'frame_options':'frame_options' in features, 'frame_ack':'frame_ack' in features,
            'stream_credit':'stream_credit' in features and 'stream_progress' in features}
