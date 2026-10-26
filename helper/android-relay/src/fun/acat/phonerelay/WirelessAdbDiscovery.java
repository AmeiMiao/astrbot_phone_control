package fun.acat.phonerelay;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import java.io.IOException;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** 仅解析属于本机接口的无线调试连接服务，配对端口不作为控制端口。 */
final class WirelessAdbDiscovery {
    static final class Endpoint {
        final String host; final int port;
        Endpoint(String host,int port) { this.host=host;this.port=port; }
    }
    static Endpoint find(Context context) throws Exception {
        if(Build.VERSION.SDK_INT<30)throw new IOException("无线调试发现需要Android 11及以上");
        NsdManager manager=context.getSystemService(NsdManager.class);
        CountDownLatch done=new CountDownLatch(1);
        Endpoint[] result=new Endpoint[1];String[] error={"未发现本机连接端口；请开启无线调试，或手填系统首页连接端口"};
        AtomicBoolean closed=new AtomicBoolean();
        java.util.concurrent.ConcurrentLinkedQueue<NsdServiceInfo> pending=new java.util.concurrent.ConcurrentLinkedQueue<>();
        AtomicBoolean resolving=new AtomicBoolean();
        Runnable resolveNext=new Runnable() {
            public void run() {
                if(closed.get() || !resolving.compareAndSet(false,true))return;
                NsdServiceInfo next=pending.poll();
                if(next==null) {resolving.set(false);return;}
                Runnable finished=()-> {resolving.set(false);run();};
                try { manager.resolveService(next,new NsdManager.ResolveListener() {
                    public void onResolveFailed(NsdServiceInfo info,int code) {
                        error[0]="本机端口解析失败："+code+"；可重试或手填连接端口";finished.run();
                    }
                    public void onServiceResolved(NsdServiceInfo info) {
                        try {
                            InetAddress host=info.getHost();int port=info.getPort();
                            if(!closed.get() && host!=null && NetworkInterface.getByInetAddress(host)!=null && port>0 && port<=65535) {
                                result[0]=new Endpoint(host.getHostAddress(),port);done.countDown();
                            }
                        } catch(IOException e) {error[0]="检查本机接口失败："+e.getMessage();}
                        finally {finished.run();}
                    }
                }); } catch(IllegalArgumentException e) {error[0]="系统解析服务不可用："+e.getMessage();finished.run();}
            }
        };
        NsdManager.DiscoveryListener discovery=new NsdManager.DiscoveryListener() {
            public void onDiscoveryStarted(String type) { }
            public void onDiscoveryStopped(String type) { }
            public void onStartDiscoveryFailed(String type,int code) { error[0]="系统发现服务启动失败："+code;done.countDown(); }
            public void onStopDiscoveryFailed(String type,int code) { android.util.Log.w("PhoneRelay","停止发现失败："+code); }
            public void onServiceLost(NsdServiceInfo service) { }
            public void onServiceFound(NsdServiceInfo service) {
                if(closed.get())return;
                if(pending.size()<16)pending.offer(service);
                resolveNext.run();
            }
        };
        manager.discoverServices("_adb-tls-connect._tcp.",NsdManager.PROTOCOL_DNS_SD,discovery);
        try { done.await(6,TimeUnit.SECONDS); }
        finally {
            closed.set(true);
            try { manager.stopServiceDiscovery(discovery); }
            catch(IllegalArgumentException e) { android.util.Log.w("PhoneRelay","发现服务已停止",e); }
        }
        if(result[0]==null)throw new IOException(error[0]);
        return result[0];
    }
}
