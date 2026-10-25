package fun.acat.phonerelay;

import android.content.Context;
import android.util.Base64;
import java.io.*;
import java.util.concurrent.*;
import java.security.KeyStore;
import javax.net.ssl.*;
import java.util.concurrent.atomic.AtomicLong;
import org.json.*;

/** 手机代理及有界流量队列，状态与插件控制连接共同管理。 */
final class PhoneTraffic implements AutoCloseable {
    private final Context context;
    private final LocalAdb adb;
    private ProxyCertificates certificates;
    private PhoneHttpProxy proxy;
    private AppProxyTunnel tunnel;
    private volatile String capture = "";
    private String provider = "app", previous = "", expected = "";
    private final ArrayBlockingQueue<JSONObject> records = new ArrayBlockingQueue<>(64);
    private final AtomicLong count = new AtomicLong(), dropped = new AtomicLong();
    private volatile String error = "";
    private long lastReport;
    PhoneTraffic(Context context, LocalAdb adb) { this.context = context; this.adb = adb; }
    synchronized JSONObject prepare() throws Exception {
        if (certificates == null) certificates = new ProxyCertificates(context.getFilesDir());
        return new JSONObject().put("ca", certificates.pem());
    }
    synchronized JSONObject start(JSONObject params, AppProxyTunnel.Dial dial) throws Exception {
        stop();
        int port = params.getInt("port"); String id = params.getString("capture_id");
        if (port < 1024 || port > 65535 || !id.matches("[0-9a-f]{32}")) throw new IOException("抓包参数无效");
        provider = params.getString("provider");
        if (!provider.equals("app") && !provider.equals("mitmproxy")) throw new IOException("抓包方式无效");
        capture = id; error = ""; count.set(0); dropped.set(0); records.clear();
        try {
            if (provider.equals("app")) {
                prepare();
                proxy = new PhoneHttpProxy(certificates, params.getInt("body_chars"), (method, url, status, type, request, body) -> {
                    if (!id.equals(capture)) return;
                    try {
                        JSONObject record = new JSONObject().put("method", method).put("url", url).put("status", status)
                            .put("ctype", type.substring(0, Math.min(512, type.length()))).put("reqbody", request).put("body", body);
                        count.incrementAndGet(); if (!records.offer(record)) dropped.incrementAndGet();
                    } catch (JSONException e) { error = "抓包记录编码失败"; }
                }, trustedUpstream()); proxy.start(port);
            } else { tunnel = new AppProxyTunnel(dial); tunnel.start(port); }
            previous = command("settings get global http_proxy").trim();
            expected = "127.0.0.1:" + port;
            command("settings put global http_proxy " + quote(expected));
            if (!command("settings get global http_proxy").trim().equals(expected)) throw new IOException("手机代理设置未生效");
            return status();
        } catch (Exception e) { stop(); throw e; }
    }
    synchronized JSONObject status() throws JSONException {
        boolean active = proxy != null && proxy.running() || tunnel != null && tunnel.running();
        int failures = proxy != null ? proxy.failures() : tunnel != null ? tunnel.failures() : 0;
        String detail = proxy != null ? proxy.lastError() : tunnel != null ? tunnel.lastError() : "";
        return new JSONObject().put("running", active).put("capture_id", capture).put("provider", provider)
            .put("flows", count.get()).put("dropped", dropped.get()).put("failures", failures).put("error", error.isEmpty() ? detail : error);
    }
    synchronized JSONObject drain() throws JSONException {
        if (capture.isEmpty()) return null;
        long now = android.os.SystemClock.elapsedRealtime();
        if (records.isEmpty() && now - lastReport < 10000) return null;
        lastReport = now;
        JSONArray batch = new JSONArray();
        for (int i = 0; i < 8; i++) { JSONObject row = records.poll(); if (row == null) break; batch.put(row); }
        return new JSONObject().put("type", "traffic").put("capture_id", capture).put("records", batch).put("dropped", dropped.get()).put("failures", proxy != null ? proxy.failures() : tunnel != null ? tunnel.failures() : 0)
            .put("error", proxy != null ? proxy.lastError() : tunnel != null ? tunnel.lastError() : "");
    }
    synchronized void stopOwned(String id) throws Exception {
        if (!capture.isEmpty() && !capture.equals(id)) throw new IOException("抓包会话已变更");
        stop();
    }
    synchronized void stop() throws Exception {
        String failure = "";
        if (!expected.isEmpty()) {
            try {
                if (command("settings get global http_proxy").trim().equals(expected))
                    command("settings put global http_proxy " + quote(previous.isEmpty() || previous.equals("null") ? ":0" : previous));
            } catch (Exception e) { failure = "恢复手机代理失败：" + e.getClass().getSimpleName(); }
        }
        expected = "";
        if (proxy != null) proxy.close(); if (tunnel != null) tunnel.close();
        proxy = null; tunnel = null; capture = ""; records.clear();
        if (!failure.isEmpty()) { error = failure; throw new IOException(failure); }
    }
    private static SSLSocketFactory trustedUpstream() throws Exception {
        KeyStore roots = KeyStore.getInstance("AndroidCAStore"); roots.load(null, null);
        TrustManagerFactory trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()); trust.init(roots);
        SSLContext tls = SSLContext.getInstance("TLS"); tls.init(null, trust.getTrustManagers(), null);
        return tls.getSocketFactory();
    }
    private String command(String command) throws Exception {
        JSONObject result = LocalAdb.bounded("配置手机抓包代理", 3000, () -> adb.shell("traffic", command, 8192));
        if (result.getInt("rc") != 0) throw new IOException("手机代理命令失败");
        return new String(Base64.decode(result.getString("stdout"), Base64.NO_WRAP), "UTF-8");
    }
    private static String quote(String value) { return "'" + value.replace("'", "'\\''") + "'"; }
    public void close() { try { stop(); } catch (Exception e) { android.util.Log.w("PhoneRelay", "抓包收尾失败，等待设备租约恢复", e); } }
}
