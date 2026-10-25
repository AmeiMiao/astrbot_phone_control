package fun.acat.phonerelay;

import android.app.*;
import android.content.*;
import android.os.*;
import android.util.Base64;
import java.io.*;
import java.net.*;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ssl.*;
import org.json.JSONObject;

/** App 主动建立固定证书 TLS 连接，本机 ADB 执行请求。 */
public final class ControlService extends Service {
    public static volatile String status = "App 控制已停止";
    public static volatile boolean connected;
    public static volatile JSONObject updateInfo;
    private volatile boolean running;
    private volatile SSLSocket socket;
    private volatile LocalAdb adb;
    private volatile DataOutputStream output;
    private volatile String session;
    private ExecutorService network;
    private ExecutorService work;
    private ScheduledExecutorService timers;
    private final ConcurrentHashMap<String, FutureTask<Void>> jobs = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, CompletableFuture<JSONObject>> downloads = new ConcurrentHashMap<>();
    private long lastRequestSequence;

    private static void requireLocal(String host) throws Exception {
        InetAddress address = InetAddress.getByName(host);
        if (!address.isLoopbackAddress() && NetworkInterface.getByInetAddress(address) == null)
            throw new IOException("ADB 地址必须属于本机");
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || "stop".equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        String action = intent.getAction();
        if ("check".equals(action) || "install".equals(action)) {
            if (work == null || socket == null) { status = "请先连接插件"; return START_NOT_STICKY; }
            work.execute(() -> {
                try {
                    if (!jobs.isEmpty()) throw new IOException("请先停止手机任务再更新");
                    if ("check".equals(action)) {
                        updateInfo = requestUpdate(-1).getJSONObject("info");
                        long installed = UpdateInstaller.version(getPackageManager().getPackageInfo(getPackageName(), 0));
                        status = updateInfo.getInt("versionCode") > installed
                            ? "插件提供版本 " + updateInfo.getString("versionName") + "；下载后点击确认系统安装"
                            : "当前已是插件提供的版本 " + updateInfo.getString("versionName");
                    } else installUpdate();
                } catch (Exception e) { status = "更新失败：" + e.getMessage(); }
            });
            return START_NOT_STICKY;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("control", "手机 App 控制", NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop = PendingIntent.getService(this, 31, new Intent(this, ControlService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        startForeground(31, new Notification.Builder(this, "control").setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("Phone ADB Relay").setContentText("本机 ADB 控制已启用")
            .addAction(new Notification.Action.Builder(null, "停止", stop).build()).build());
        if (running) return START_NOT_STICKY;
        running = true;
        network = Executors.newSingleThreadExecutor();
        work = Executors.newFixedThreadPool(2);
        timers = Executors.newScheduledThreadPool(2);
        network.execute(this::connectLoop);
        return START_NOT_STICKY;
    }
    private void connectLoop() {
        android.content.SharedPreferences prefs = getSharedPreferences("control", 0);
        while (running) {
            try {
                String host = prefs.getString("adb_host", "127.0.0.1");
                requireLocal(host);
                adb = new LocalAdb(this);
                if (!adb.connect(host, prefs.getInt("adb_port", 5555))) throw new IOException("本机 ADB 未授权或未连接");
                JSONObject code = new JSONObject(new String(Base64.decode(prefs.getString("code", ""), Base64.URL_SAFE), "UTF-8"));
                if (code.getInt("v") != 1) throw new IOException("配对码版本不支持");
                final String pin = code.getString("pin");
                if (!pin.matches("[0-9a-f]{64}")) throw new IOException("证书指纹无效");
                TrustManager[] trust = new TrustManager[]{new X509TrustManager() {
                    public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                    public void checkClientTrusted(X509Certificate[] chain, String auth) throws CertificateException { throw new CertificateException("仅连接服务端"); }
                    public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                        try {
                            chain[0].checkValidity();
                            String actual = hex(MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded()));
                            if (!MessageDigest.isEqual(actual.getBytes("US-ASCII"), pin.getBytes("US-ASCII"))) throw new CertificateException("插件证书指纹不匹配");
                        } catch (CertificateException e) { throw e; }
                        catch (Exception e) { throw new CertificateException(e); }
                    }
                }};
                SSLContext tls = SSLContext.getInstance("TLS"); tls.init(null, trust, new SecureRandom());
                SSLSocket active = (SSLSocket) tls.getSocketFactory().createSocket(); socket = active;
                active.connect(new InetSocketAddress(code.getString("host"), code.getInt("port")), 10000);
                active.setSoTimeout(45000); active.setTcpNoDelay(true); active.startHandshake();
                output = new DataOutputStream(active.getOutputStream());
                DataInputStream input = new DataInputStream(active.getInputStream());
                String device = prefs.getString("device_id", "");
                if (device.isEmpty()) { device = UUID.randomUUID().toString(); prefs.edit().putString("device_id", device).apply(); }
                send(new JSONObject().put("v", 1).put("token", code.getString("token")).put("device_id", device)
                    .put("version", getPackageManager().getPackageInfo(getPackageName(), 0).versionName));
                JSONObject ready = read(input);
                if (!"ready".equals(ready.optString("type"))) throw new IOException("插件配对被拒绝");
                session = ready.getString("session"); lastRequestSequence = 0;
                connected = true;
                status = "App 控制已连接；本机 ADB 权限：shell";
                ScheduledFuture<?> heartbeat = timers.scheduleAtFixedRate(() -> {
                    try { send(new JSONObject().put("type", "ping")); }
                    catch (Exception e) { closeSocket(); }
                }, 10, 10, TimeUnit.SECONDS);
                try { while (running && socket == active) handle(read(input)); }
                finally { heartbeat.cancel(false); }
            } catch (Exception e) {
                if (running) status = "连接失败：" + e.getMessage() + "；等待重新连接";
            } finally { cleanupConnection(); }
            if (running) try { Thread.sleep(3000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
        }
    }
    private void handle(JSONObject message) throws Exception {
        String type = message.optString("type"), id = message.optString("id");
        if ("pong".equals(type)) return;
        if ("update".equals(type)) {
            CompletableFuture<JSONObject> future = downloads.get(id);
            if (future != null) future.complete(message);
            return;
        }
        if (!session.equals(message.optString("session"))) throw new IOException("会话已失效");
        if ("cancel".equals(type)) { cancel(id); return; }
        long requestSequence = message.getLong("seq");
        if (!"call".equals(type) || !id.matches("[0-9a-f]{32}") || requestSequence <= lastRequestSequence)
            throw new IOException("重复或无效请求");
        lastRequestSequence = requestSequence;
        long ttl = message.getLong("ttl_ms");
        if (ttl <= 0 || ttl > 300000 || jobs.size() >= 8) throw new IOException("请求期限或并发无效");
        final long deadline = SystemClock.elapsedRealtime() + ttl;
        final String requestSession = session;
        final LocalAdb local = adb;
        FutureTask<Void> task = new FutureTask<>(() -> {
            ScheduledFuture<?> expiry = null;
            JSONObject result = new JSONObject().put("type", "result").put("id", id);
            try {
                if (SystemClock.elapsedRealtime() >= deadline || !requestSession.equals(session)) throw new IOException("请求已过期");
                expiry = timers.schedule(() -> cancel(id), Math.max(1, deadline - SystemClock.elapsedRealtime()), TimeUnit.MILLISECONDS);
                JSONObject params = message.getJSONObject("params"), data;
                switch (message.getString("op")) {
                    case "shell": data = local.shell(id, params.getString("command"), Math.min(8 * 1024 * 1024, params.optInt("limit", 8192))); break;
                    case "nodes": data = local.nodes(id, 8 * 1024 * 1024); break;
                    case "screen": data = local.screen(); break;
                    case "clipboard": data = local.clipboard(); break;
                    case "input": data = local.input(id, params.getString("text"), params.optBoolean("press_enter")); break;
                    default: throw new IOException("不支持的控制能力");
                }
                for (Iterator<String> keys = data.keys(); keys.hasNext();) { String key = keys.next(); result.put(key, data.get(key)); }
            } catch (Exception e) { result.put("error", e.getMessage() == null ? "本机 ADB 请求失败" : e.getMessage()); }
            finally { if (expiry != null) expiry.cancel(false); jobs.remove(id); }
            if (requestSession.equals(session) && !Thread.currentThread().isInterrupted()) send(result);
            return null;
        });
        jobs.put(id, task); work.execute(task);
    }
    private void cancel(String id) {
        FutureTask<Void> task = jobs.remove(id);
        if (task != null) task.cancel(true);
        LocalAdb local = adb; if (local != null) local.cancel(id);
    }
    static JSONObject read(DataInputStream input) throws Exception {
        int size = input.readInt();
        if (size < 1 || size > 12 * 1024 * 1024) throw new IOException("消息长度无效");
        byte[] data = new byte[size]; input.readFully(data);
        return new JSONObject(new String(data, "UTF-8"));
    }
    private synchronized void send(JSONObject message) throws Exception {
        byte[] data = message.toString().getBytes("UTF-8");
        DataOutputStream out = output;
        if (out == null) throw new IOException("插件未连接");
        out.writeInt(data.length); out.write(data); out.flush();
    }
    private JSONObject requestUpdate(int offset) throws Exception {
        String id = UUID.randomUUID().toString();
        CompletableFuture<JSONObject> future = new CompletableFuture<>(); downloads.put(id, future);
        try {
            JSONObject request = new JSONObject().put("type", "update").put("id", id);
            if (offset >= 0) request.put("offset", offset);
            send(request); return future.get(30, TimeUnit.SECONDS);
        } finally { downloads.remove(id); }
    }
    private void installUpdate() throws Exception {
        JSONObject info = requestUpdate(-1).getJSONObject("info");
        if (info.getInt("versionCode") <= UpdateInstaller.version(getPackageManager().getPackageInfo(getPackageName(), 0)))
            throw new IOException("当前已是插件提供的版本");
        int size = info.getInt("size");
        if (size < 1 || size > 32 * 1024 * 1024) throw new IOException("安装包大小无效");
        File file = new File(getCacheDir(), "relay-update.apk");
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        try (OutputStream out = new FileOutputStream(file)) {
            for (int offset = 0; offset < size;) {
                JSONObject response = requestUpdate(offset);
                if (response.getInt("offset") != offset || !response.getJSONObject("info").toString().equals(info.toString())) throw new IOException("更新包已变化，请重新检查");
                byte[] data = Base64.decode(response.getString("data"), Base64.NO_WRAP);
                if (data.length == 0 || offset + data.length > size) throw new IOException("更新包不完整");
                out.write(data); hash.update(data); offset += data.length;
            }
        }
        if (!hex(hash.digest()).equals(info.getString("sha256"))) { file.delete(); throw new IOException("安装包摘要校验失败"); }
        UpdateInstaller.prepare(this, file, info.getInt("versionCode"));
        status = "安装包已验证，请在 App 中确认系统安装";
    }
    static String hex(byte[] data) {
        StringBuilder b = new StringBuilder(); for (byte value : data) b.append(String.format(Locale.ROOT, "%02x", value & 255)); return b.toString();
    }
    private void closeSocket() { SSLSocket s = socket; socket = null; if (s != null) try { s.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭连接失败", e); } }
    private void cleanupConnection() {
        connected = false;
        session = null; closeSocket(); output = null;
        for (String id : jobs.keySet()) cancel(id);
        for (CompletableFuture<JSONObject> future : downloads.values()) future.completeExceptionally(new IOException("插件已断线"));
        LocalAdb local = adb; adb = null;
        if (local != null) try { local.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭本机 ADB 失败", e); }
    }
    @Override public void onDestroy() {
        running = false; cleanupConnection();
        if (network != null) network.shutdownNow();
        if (work != null) work.shutdownNow();
        if (timers != null) timers.shutdownNow();
        status = "App 控制已停止"; super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
