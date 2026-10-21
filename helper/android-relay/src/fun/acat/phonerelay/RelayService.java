package fun.acat.phonerelay;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 通过 App UID 把隧道 TCP 连接转给 loopback adbd，保留 ADB 协议认证。 */
public final class RelayService extends Service {
    public static volatile String status = "中转已停止";
    private volatile ServerSocket listener;
    private ExecutorService workers;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private volatile boolean running;

    private static InetAddress ipv4(String value) throws IOException {
        if (value == null || !value.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}"))
            throw new IOException("必须填写 IPv4 地址");
        for (String part : value.split("\\."))
            if (Integer.parseInt(part) > 255) throw new IOException("IPv4 地址无效");
        InetAddress address = InetAddress.getByName(value);
        if (address.isAnyLocalAddress() || address.isLoopbackAddress() || address.isMulticastAddress())
            throw new IOException("必须使用 WireGuard 单播地址");
        return address;
    }

    private static void requireTunnel(InetAddress address) throws IOException {
        NetworkInterface n = NetworkInterface.getByInetAddress(address);
        if (n == null || !n.isUp() || !(n.getName().startsWith("tun") || n.getName().startsWith("wg")))
            throw new IOException("监听地址必须属于已连接的 tun/wg 接口");
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("relay", "WireGuard ADB 中转", NotificationManager.IMPORTANCE_LOW));
        Intent stop = new Intent(this, RelayService.class).setAction("stop");
        PendingIntent stopAction = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, "relay")
            .setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("Phone WG Relay")
            .setContentText("仅隧道内允许的控制端可连接；点击停止可立即回收连接")
            .addAction(new Notification.Action.Builder(null, "停止", stopAction).build()).build();
        startForeground(13, notification);
        if (intent == null || "stop".equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        shutdown();
        running = true;
        workers = Executors.newFixedThreadPool(33);
        final ExecutorService pool = workers;
        String address = intent.getStringExtra("address");
        String peer = intent.getStringExtra("peer");
        pool.execute(() -> {
            try {
                InetAddress bind = ipv4(address);
                InetAddress allowed = ipv4(peer);
                requireTunnel(bind);
                ServerSocket server = new ServerSocket();
                listener = server;
                server.setReuseAddress(true);
                server.bind(new InetSocketAddress(bind, 15555), 16);
                server.setSoTimeout(1000);
                status = "中转运行 " + address + ":15555 → 127.0.0.1:5555；允许 " + peer;
                while (running) {
                    requireTunnel(bind);
                    Socket incoming;
                    try { incoming = server.accept(); } catch (SocketTimeoutException timeout) { continue; }
                    if (!allowed.equals(incoming.getInetAddress()) || sockets.size() >= 32) {
                        close(incoming); continue;
                    }
                    sockets.add(incoming);
                    pool.execute(() -> relay(incoming, pool));
                }
            } catch (Exception e) {
                status = "中转结束：" + e.getMessage();
            } finally {
                // startId 防止前一代监听任务停止新一代服务。
                if (workers == pool) stopSelf(startId);
            }
        });
        return START_NOT_STICKY;
    }

    private void relay(Socket incoming, ExecutorService pool) {
        Socket local = new Socket();
        sockets.add(local);
        try {
            local.connect(new InetSocketAddress("127.0.0.1", 5555), 5000);
            local.setTcpNoDelay(true);
            incoming.setTcpNoDelay(true);
            pool.execute(() -> pipe(local, incoming));
            pipe(incoming, local);
        } catch (IOException e) {
            status = "ADB 连接失败：" + e.getMessage();
        } finally {
            close(local); close(incoming);
            sockets.remove(local); sockets.remove(incoming);
        }
    }

    private void pipe(Socket source, Socket target) {
        try {
            InputStream input = source.getInputStream();
            OutputStream output = target.getOutputStream();
            byte[] buffer = new byte[32768];
            int count;
            while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
        } catch (IOException e) {
            android.util.Log.d("PhoneRelay", "TCP 通道结束：" + e.getMessage());
        } finally { close(source); close(target); }
    }

    private static void close(java.io.Closeable value) {
        if (value != null) try { value.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭连接失败", e); }
    }

    private synchronized void shutdown() {
        running = false;
        close(listener); listener = null;
        for (Socket socket : sockets) close(socket);
        sockets.clear();
        if (workers != null) workers.shutdownNow();
    }

    @Override public void onDestroy() { shutdown(); status = "中转已停止"; super.onDestroy(); }
    @Override public IBinder onBind(Intent intent) { return null; }
}
