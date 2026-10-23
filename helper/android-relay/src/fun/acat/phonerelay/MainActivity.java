package fun.acat.phonerelay;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 用户确认隧道监听地址和唯一允许的控制端后启动中转。 */
public final class MainActivity extends Activity {
    private EditText address;
    private EditText peer;
    private TextView status;
    private final android.os.Handler handler = new android.os.Handler();
    private final Runnable refresh = new Runnable() {
        public void run() {
            status.setText(RelayService.status);
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
        setContentView(box);
        // 自动化入口仍通过可见 Activity，服务不向其它应用导出。
        if (getIntent().hasExtra("address") && getIntent().hasExtra("peer")) {
            address.setText(getIntent().getStringExtra("address"));
            peer.setText(getIntent().getStringExtra("peer"));
            if (getIntent().getBooleanExtra("start", false)) startRelay();
        }
    }

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
