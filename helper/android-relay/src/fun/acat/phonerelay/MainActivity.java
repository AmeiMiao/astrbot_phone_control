package fun.acat.phonerelay;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.concurrent.Executors;

/** 手机控制连接、无线配对、更新与组网中转的可见设置入口。 */
public final class MainActivity extends Activity {
    private RelayUi ui;
    private Bundle initialState;
    private EditText address, peer, adbHost, adbPort, pairPort, pairCode, pluginCode;
    private TextView status, statusBadge, pairAddress;
    private Button startControl, checkUpdate, downloadUpdate;
    private ScrollView scroll;
    private final LinearLayout[] pages = new LinearLayout[4];
    private final Button[] tabs = new Button[4];
    private int selectedPage;
    private final java.util.concurrent.ExecutorService setupWorker = Executors.newSingleThreadExecutor();
    private final android.os.Handler handler = new android.os.Handler();
    private final Runnable refresh = new Runnable() {
        public void run() { renderStatus(); handler.postDelayed(this, 1000); }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state); initialState = state; ui = new RelayUi(this);
        boolean compact = getResources().getConfiguration().screenHeightDp < 480;
        getWindow().setStatusBarColor(ui.background); getWindow().setNavigationBarColor(ui.background);
        getWindow().getDecorView().setSystemUiVisibility(ui.dark ? 0
            : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        getWindow().setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE | android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        LinearLayout root = ui.vertical(); root.setBackgroundColor(ui.background);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom()); return insets;
        });
        LinearLayout heading = new LinearLayout(this); heading.setGravity(Gravity.CENTER_VERTICAL);
        heading.setPadding(ui.dp(20), ui.dp(compact ? 8 : 18), ui.dp(20), ui.dp(compact ? 8 : 16));
        TextView mark = ui.text("R", 23, ui.accent, true); mark.setGravity(Gravity.CENTER);
        mark.setBackground(ui.rounded(ui.tint, 0, 14));
        heading.addView(mark, new LinearLayout.LayoutParams(ui.dp(compact ? 34 : 46), ui.dp(compact ? 34 : 46)));
        LinearLayout names = ui.vertical(); names.setPadding(ui.dp(14), 0, ui.dp(8), 0);
        names.addView(ui.text("Phone Relay", compact ? 20 : 23, ui.text, true));
        if (!compact) names.addView(ui.text("手机控制与连接", 12, ui.muted, false));
        heading.addView(names, new LinearLayout.LayoutParams(0, -2, 1));
        heading.addView(ui.text("v" + appVersion(), 12, ui.muted, false)); root.addView(heading);
        LinearLayout top = ui.vertical(); top.setPadding(ui.dp(20), 0, ui.dp(20), 0);
        LinearLayout connection;
        if (compact) {
            connection = ui.vertical(); connection.setPadding(ui.dp(12), ui.dp(8), ui.dp(12), 0);
            connection.setBackground(ui.rounded(ui.panel, ui.border, 12)); ui.add(top, connection, 8);
        } else connection = ui.card(top, "连接状态", "");
        statusBadge = ui.text("待连接", 12, ui.muted, true); ui.add(connection, statusBadge, 5);
        status = ui.text("", 13, ui.muted, false);
        if (compact) { status.setMaxLines(2); status.setEllipsize(android.text.TextUtils.TruncateAt.END); }
        ui.add(connection, status, compact ? 8 : 12); root.addView(top);
        LinearLayout navigation = new LinearLayout(this);
        navigation.setPadding(ui.dp(20), 0, ui.dp(20), ui.dp(compact ? 6 : 12));
        String[] labels = {"连接", "无线配对", "更新", "中转"};
        for (int i = 0; i < tabs.length; i++) {
            final int page = i; Button tab = new Button(this); tab.setText(labels[i]);
            tab.setAllCaps(false); tab.setTextSize(13); tab.setMinWidth(0); tab.setMinimumWidth(0);
            tab.setPadding(0, 0, 0, 0); tab.setBackgroundTintList(null);
            tab.setOnClickListener(v -> selectPage(page)); tabs[i] = tab;
            LinearLayout.LayoutParams size = new LinearLayout.LayoutParams(0, ui.dp(44), 1);
            if (i > 0) size.leftMargin = ui.dp(5); navigation.addView(tab, size);
        }
        root.addView(navigation); scroll = new ScrollView(this); scroll.setId(0x7f010001); scroll.setFillViewport(true);
        scroll.setClipToPadding(false); scroll.setPadding(ui.dp(20), 0, ui.dp(20), ui.dp(8));
        FrameLayout holder = new FrameLayout(this);
        for (int i = 0; i < pages.length; i++) { pages[i] = ui.vertical(); holder.addView(pages[i]); }
        scroll.addView(holder); root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        addControlSettings(); addPairingSettings(); addUpdateSettings(); addRelaySettings();
        setContentView(root); root.requestApplyInsets();
        selectPage(state == null ? 0 : state.getInt("page", 0));
        if (getIntent().hasExtra("address") && getIntent().hasExtra("peer")) {
            address.setText(getIntent().getStringExtra("address"));
            peer.setText(getIntent().getStringExtra("peer")); selectPage(3);
            if (getIntent().getBooleanExtra("start", false)) startRelay();
        }
    }
    private String restored(String key, String fallback) {
        return initialState == null ? fallback : initialState.getString(key, fallback);
    }
    private String appVersion() {
        try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; }
        catch (Exception e) { return "—"; }
    }
    private void selectPage(int page) {
        selectedPage = page < 0 || page >= pages.length ? 0 : page;
        for (int i = 0; i < pages.length; i++) {
            pages[i].setVisibility(i == selectedPage ? View.VISIBLE : View.GONE);
            tabs[i].setSelected(i == selectedPage);
            tabs[i].setTextColor(i == selectedPage ? ui.accent : ui.muted);
            tabs[i].setBackground(ui.rounded(i == selectedPage ? ui.tint : ui.background, 0, 10));
        }
        pairAddress.setText("本机地址：" + adbHost.getText().toString().trim() + "（可在连接页修改）");
        scroll.scrollTo(0, 0); renderStatus();
    }
    private void renderStatus() {
        if (status == null) return;
        String summary = selectedPage == 3 ? RelayService.status : ControlService.status;
        if (!summary.contentEquals(status.getText())) status.setText(summary);
        boolean connected = ControlService.status.startsWith("App 控制已连接");
        String badge = selectedPage == 3 ? "组网中转" : connected ? "已连接" : "待连接";
        if (!badge.contentEquals(statusBadge.getText())) statusBadge.setText(badge);
        statusBadge.setTextColor(connected && selectedPage != 3 ? ui.green : ui.muted);
        startControl.setEnabled(!connected); startControl.setAlpha(connected ? .55f : 1f);
        checkUpdate.setEnabled(connected); downloadUpdate.setEnabled(connected);
        checkUpdate.setAlpha(connected ? 1f : .55f); downloadUpdate.setAlpha(connected ? 1f : .55f);
    }
    private int port(EditText input, String label) {
        try {
            int value = Integer.parseInt(input.getText().toString().trim());
            if (value > 0 && value <= 65535) return value;
        } catch (NumberFormatException e) { /* 统一展示字段校验结果。 */ }
        input.setError("请输入 1–65535 的端口"); throw new IllegalArgumentException(label + "无效");
    }
    private void addControlSettings() {
        SharedPreferences prefs = getSharedPreferences("control", 0);
        LinearLayout card = ui.card(pages[0], "连接插件", "在插件的 Relay App 页面获取配对码，填写后启动控制。");
        pluginCode = ui.field(card, "插件配对码", "粘贴插件提供的配对码", restored("code", prefs.getString("code", "")), true, false);
        LinearLayout local = ui.card(pages[0], "本机 ADB", "支持本机 TCP ADB，也可使用无线调试的连接端口。");
        adbHost = ui.field(local, "本机 ADB 地址", "127.0.0.1", restored("adb_host", prefs.getString("adb_host", "127.0.0.1")), false, false);
        adbPort = ui.field(local, "ADB 连接端口", "与配对端口不同", restored("adb_port", String.valueOf(prefs.getInt("adb_port", 5555))), false, true);
        startControl = ui.button(local, "启动 App 控制", true, () -> {
            try {
                int number = port(adbPort, "ADB 连接端口");
                if (pluginCode.getText().toString().trim().isEmpty()) { pluginCode.setError("请填写插件配对码"); return; }
                prefs.edit().putString("adb_host", adbHost.getText().toString().trim()).putInt("adb_port", number)
                    .putString("code", pluginCode.getText().toString().trim()).apply();
                startForegroundService(new Intent(this, ControlService.class));
            } catch (Exception e) { ControlService.status = "配置错误：" + e.getMessage(); renderStatus(); }
        });
        ui.button(local, "停止 App 控制", false, () -> stopService(new Intent(this, ControlService.class)));
        ui.add(local, ui.text("修改参数后，先停止再启动控制。音视频、文件与抓包使用插件直连模式。", 12, ui.muted, false), 12);
    }
    private void addPairingSettings() {
        LinearLayout guide = ui.card(pages[1], "无线调试配对", "Android 11 及以上可使用六位配对码授权本机 ADB。");
        ui.add(guide, ui.text("1  开启系统无线调试\n2  分屏打开系统的“使用配对码配对”\n3  及时填写配对端口和六位码", 13, ui.muted, false), 16);
        ui.button(guide, "打开开发者选项", false, () -> {
            try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)); }
            catch (android.content.ActivityNotFoundException e) { ControlService.status = "请从系统设置打开开发者选项"; renderStatus(); }
        });
        LinearLayout card = ui.card(pages[1], "填写本次配对信息", "保持系统配对窗口打开，完成后回连接页填写系统首页的连接端口。");
        pairAddress = ui.text("", 12, ui.muted, false); ui.add(card, pairAddress, 16);
        pairPort = ui.field(card, "配对端口", "系统配对弹窗中的端口", restored("pair_port", ""), false, true);
        pairCode = ui.field(card, "六位配对码", "系统配对弹窗中的验证码", restored("pair_code", ""), true, true);
        pairCode.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(6)});
        ui.button(card, "无线调试配对", true, () -> {
            try {
                final String host = adbHost.getText().toString().trim(); final int number = port(pairPort, "配对端口");
                final String code = pairCode.getText().toString().trim();
                if (android.os.Build.VERSION.SDK_INT < 30) throw new IllegalArgumentException("无线配对需 Android 11 及以上");
                if (!code.matches("[0-9]{6}")) { pairCode.setError("请输入六位配对码"); return; }
                pairCode.setText(""); ControlService.status = "正在进行无线配对…"; renderStatus();
                setupWorker.execute(() -> {
                    try (LocalAdb local = new LocalAdb(this)) {
                        java.net.InetAddress address = java.net.InetAddress.getByName(host);
                        if (!address.isLoopbackAddress() && java.net.NetworkInterface.getByInetAddress(address) == null)
                            throw new java.io.IOException("ADB 地址必须属于本机");
                        ControlService.status = local.pair(host, number, code)
                            ? "无线配对完成，请在连接页填写连接端口并启动控制" : "无线配对失败";
                    } catch (Exception e) { ControlService.status = "配对失败：" + e.getMessage(); }
                    handler.post(() -> { if (!isDestroyed()) renderStatus(); });
                });
            } catch (Exception e) { ControlService.status = "配对设置错误：" + e.getMessage(); renderStatus(); }
        });
    }
    private void addUpdateSettings() {
        LinearLayout card = ui.card(pages[2], "App 更新", "当前版本 " + appVersion() + "\n更新包由已连接插件提供，验证后由你确认系统安装。");
        checkUpdate = ui.button(card, "检查 App 更新", true, () -> startService(new Intent(this, ControlService.class).setAction("check")));
        downloadUpdate = ui.button(card, "下载更新包", false, () -> startService(new Intent(this, ControlService.class).setAction("install")));
        ui.button(card, "确认系统安装", false, () -> {
            try {
                if (InstallReceiver.confirmation != null) { startActivity(InstallReceiver.confirmation); InstallReceiver.confirmation = null; }
                else UpdateInstaller.install(this);
            } catch (Exception e) { ControlService.status = "安装失败：" + e.getMessage(); renderStatus(); }
        });
        ui.add(card, ui.text("连接插件后可检查更新。更新完成后重新打开 App 并启动控制。", 12, ui.muted, false), 12);
        LinearLayout about = ui.card(pages[2], "关于 Phone Relay", "本机控制、屏幕截图、UI 节点与剪贴板。");
        ui.button(about, "组件许可", false, this::showLicenses);
    }
    private void addRelaySettings() {
        LinearLayout card = ui.card(pages[3], "组网 TCP 中转", "将本机 ADB:5555 转发到组网地址的 15555 端口，仅接受指定控制端。");
        address = ui.field(card, "本机组网 IPv4", "手机的 WireGuard / Tailscale / ZeroTier 地址", restored("address", getPreferences(0).getString("address", "")), false, false);
        peer = ui.field(card, "唯一允许的控制端 IPv4", "插件所在控制端的组网地址", restored("peer", getPreferences(0).getString("peer", "")), false, false);
        ui.button(card, "启动中转", true, this::startRelay);
        ui.button(card, "停止中转", false, () -> stopService(new Intent(this, RelayService.class)));
        ui.add(card, ui.text("先连接组网客户端并开启本机网络 ADB，在 VPN 的按应用设置中允许 Phone Relay。", 12, ui.muted, false), 12);
    }
    private void showLicenses() {
        try (java.io.InputStream notices = getAssets().open("third-party.txt"); java.io.InputStream license = getAssets().open("LGPL-3.0.txt")) {
            String text = new String(LocalAdb.readBounded(notices, 65536), "UTF-8") + "\n" + new String(LocalAdb.readBounded(license, 65536), "UTF-8");
            TextView content = ui.text(text, 13, ui.text, false); content.setTextIsSelectable(true); content.setPadding(ui.dp(18), ui.dp(12), ui.dp(18), ui.dp(12));
            ScrollView scroll = new ScrollView(this); scroll.addView(content);
            new android.app.AlertDialog.Builder(this, ui.dark ? android.R.style.Theme_Material_Dialog_Alert : android.R.style.Theme_Material_Light_Dialog_Alert).setTitle("组件许可").setView(scroll).setPositiveButton("关闭", null).show();
        } catch (Exception e) { ControlService.status = "读取许可失败：" + e.getMessage(); renderStatus(); }
    }
    private void startRelay() {
        String a = address.getText().toString().trim(), p = peer.getText().toString().trim();
        getPreferences(0).edit().putString("address", a).putString("peer", p).apply();
        Intent intent = new Intent(this, RelayService.class); intent.putExtra("address", a); intent.putExtra("peer", p);
        startForegroundService(intent);
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        state.putInt("page", selectedPage);
        String[] keys = {"address", "peer", "adb_host", "adb_port", "pair_port", "pair_code", "code"};
        EditText[] fields = {address, peer, adbHost, adbPort, pairPort, pairCode, pluginCode};
        for (int i = 0; i < keys.length; i++) state.putString(keys[i], fields[i].getText().toString());
        super.onSaveInstanceState(state);
    }
    @Override protected void onDestroy() { handler.removeCallbacks(refresh); setupWorker.shutdownNow(); super.onDestroy(); }
    @Override protected void onResume() { super.onResume(); handler.post(refresh); }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
}
