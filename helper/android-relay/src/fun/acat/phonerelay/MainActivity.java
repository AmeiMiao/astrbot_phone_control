package fun.acat.phonerelay;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.ScrollView;
import android.content.SharedPreferences;
import java.util.concurrent.Executors;

/** 用户确认隧道监听地址和唯一允许的控制端后启动中转。 */
public final class MainActivity extends Activity {
    private EditText address;
    private EditText peer;
    private TextView status;
    private TextView controlStatus;
    private EditText adbHost, adbPort, pairPort, pairCode, pluginCode;
    private final java.util.concurrent.ExecutorService setupWorker = Executors.newSingleThreadExecutor();
    private final android.os.Handler handler = new android.os.Handler();
    private final Runnable refresh = new Runnable() {
        public void run() {
            status.setText(RelayService.status + "\n" + ControlService.status);
            if (controlStatus != null) controlStatus.setText(ControlService.status);
            handler.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(32, 32, 32, 32);
        TextView title = new TextView(this);
        title.setText(getApplicationInfo().loadLabel(getPackageManager()));
        title.setTextSize(22);
        title.setPadding(0, 0, 0, 16);
        box.addView(title);
        TextView help = new TextView(this);
        help.setText("先连接 WireGuard、Tailscale 或 ZeroTier，并开启系统网络调试。中转使用普通 App UID，监听隧道地址:15555，仅接受下方控制端，目标固定为本机 ADB:5555。\n保留 ADB 授权；在 VPN 的按应用设置中允许本 App。");
        box.addView(help);
        address = new EditText(this);
        address.setHint("本机组网 IPv4 地址");
        address.setText(getPreferences(0).getString("address", ""));
        box.addView(address);
        peer = new EditText(this);
        peer.setHint("唯一允许的控制端组网 IPv4 地址");
        peer.setText(getPreferences(0).getString("peer", ""));
        box.addView(peer);
        Button start = new Button(this);
        start.setText("启动中转");
        start.setOnClickListener(v -> startRelay());
        box.addView(start);
        Button stop = new Button(this);
        stop.setText("停止中转");
        stop.setOnClickListener(v -> stopService(new Intent(this, RelayService.class)));
        box.addView(stop);
        status = new TextView(this);
        box.addView(status);
        addControlSettings(box);
        ScrollView scroll = new ScrollView(this); scroll.addView(box);
        setContentView(scroll);
        // 自动化入口仍通过可见 Activity，服务不向其它应用导出。
        if (getIntent().hasExtra("address") && getIntent().hasExtra("peer")) {
            address.setText(getIntent().getStringExtra("address"));
            peer.setText(getIntent().getStringExtra("peer"));
            if (getIntent().getBooleanExtra("start", false)) startRelay();
        }
    }

    private EditText field(LinearLayout box, String hint, String value, boolean secret) {
        EditText field = new EditText(this); field.setHint(hint); field.setText(value);
        if (secret) field.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        box.addView(field); return field;
    }
    private void button(LinearLayout box, String text, Runnable action) {
        Button b = new Button(this); b.setText(text); b.setOnClickListener(v -> action.run()); box.addView(b);
    }
    private void addControlSettings(LinearLayout box) {
        SharedPreferences prefs = getSharedPreferences("control", 0);
        TextView heading = new TextView(this); heading.setText("本机 ADB 与插件控制"); heading.setTextSize(20); box.addView(heading);
        TextView version = new TextView(this);
        try { version.setText("App 版本 " + getPackageManager().getPackageInfo(getPackageName(), 0).versionName); }
        catch (Exception e) { version.setText("无法读取 App 版本"); }
        box.addView(version);
        adbHost = field(box, "本机 ADB 地址", prefs.getString("adb_host", "127.0.0.1"), false);
        adbPort = field(box, "ADB 连接端口（与配对端口不同）", String.valueOf(prefs.getInt("adb_port", 5555)), false);
        pairPort = field(box, "无线调试配对端口", "", false);
        pairCode = field(box, "无线调试六位配对码", "", true);
        button(box, "无线调试配对", () -> {
            final String host = adbHost.getText().toString().trim();
            final String port = pairPort.getText().toString().trim();
            final String code = pairCode.getText().toString().trim(); pairCode.setText("");
            setupWorker.execute(() -> {
                try (LocalAdb local = new LocalAdb(this)) {
                    java.net.InetAddress address = java.net.InetAddress.getByName(host);
                    if (!address.isLoopbackAddress() && java.net.NetworkInterface.getByInetAddress(address) == null)
                        throw new java.io.IOException("ADB 地址必须属于本机");
                    if (android.os.Build.VERSION.SDK_INT < 30 || !code.matches("[0-9]{6}")) throw new java.io.IOException("需 Android 11+ 和六位配对码");
                    boolean ok = local.pair(host, Integer.parseInt(port), code);
                    ControlService.status = ok ? "无线配对完成，请填写连接端口并启动控制" : "无线配对失败";
                } catch (Exception e) { ControlService.status = "配对失败：" + e.getMessage(); }
            });
        });
        pluginCode = field(box, "Dashboard 提供的插件配对码", prefs.getString("code", ""), true);
        button(box, "启动 App 控制", () -> {
            try {
                int port = Integer.parseInt(adbPort.getText().toString().trim());
                if (port < 1 || port > 65535) throw new IllegalArgumentException("ADB 端口无效");
                prefs.edit().putString("adb_host", adbHost.getText().toString().trim()).putInt("adb_port", port)
                    .putString("code", pluginCode.getText().toString().trim()).apply();
                startForegroundService(new Intent(this, ControlService.class));
            } catch (Exception e) { ControlService.status = "配置错误：" + e.getMessage(); }
        });
        button(box, "停止 App 控制", () -> stopService(new Intent(this, ControlService.class)));
        controlStatus = new TextView(this); box.addView(controlStatus);
        button(box, "检查 App 更新", () -> startService(new Intent(this, ControlService.class).setAction("check")));
        button(box, "下载更新包", () -> startService(new Intent(this, ControlService.class).setAction("install")));
        button(box, "确认系统安装", () -> {
            try {
                if (InstallReceiver.confirmation != null) { startActivity(InstallReceiver.confirmation); InstallReceiver.confirmation = null; }
                else UpdateInstaller.install(this);
            } catch (Exception e) { ControlService.status = "安装失败：" + e.getMessage(); }
        });
        button(box, "组件许可", () -> {
            try {
                java.io.InputStream notices = getAssets().open("third-party.txt");
                java.io.InputStream license = getAssets().open("LGPL-3.0.txt");
                String text = new String(LocalAdb.readBounded(notices, 65536), "UTF-8") + "\n" + new String(LocalAdb.readBounded(license, 65536), "UTF-8");
                notices.close(); license.close();
                TextView content = new TextView(this); content.setText(text); content.setTextIsSelectable(true);
                ScrollView scroll = new ScrollView(this); scroll.addView(content);
                new android.app.AlertDialog.Builder(this).setTitle("组件许可").setView(scroll).setPositiveButton("关闭", null).show();
            } catch (Exception e) { ControlService.status = "读取许可失败：" + e.getMessage(); }
        });
        TextView note = new TextView(this);
        note.setText("本机网络调试需开启并授权。本模式提供控制、截图、节点及剪贴板；音视频、文件和抓包请在插件选择直连。插件升级后可手动检查 App 更新。"); box.addView(note);
    }
    @Override protected void onDestroy() { setupWorker.shutdownNow(); super.onDestroy(); }

    private void startRelay() {
        String a = address.getText().toString().trim();
        String p = peer.getText().toString().trim();
        getPreferences(0).edit().putString("address", a).putString("peer", p).apply();
        Intent intent = new Intent(this, RelayService.class);
        intent.putExtra("address", a);
        intent.putExtra("peer", p);
        startForegroundService(intent);
    }

    @Override protected void onResume() { super.onResume(); handler.post(refresh); }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
}
