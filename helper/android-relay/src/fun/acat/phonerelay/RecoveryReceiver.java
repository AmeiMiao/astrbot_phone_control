package fun.acat.phonerelay;

import android.content.*;

/** 用户明确启用后，重建空白控制会话；设备任务由插件重新授权。 */
public final class RecoveryReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent) {
        String action=intent.getAction();
        if(!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action))return;
        restoreIfWanted(context,action);
    }
    static void restoreIfWanted(Context context,String reason) {
        SharedPreferences prefs=context.getSharedPreferences("control",0);
        android.util.Log.i("PhoneRelay","恢复检查："+reason+"；enabled="+prefs.getBoolean("restore_enabled",false)+"；wanted="+prefs.getBoolean("control_wanted",false));
        if(!prefs.getBoolean("restore_enabled",false) || !prefs.getBoolean("control_wanted",false))return;
        if(!ControlService.connected)ControlService.status="正在恢复控制连接";
        try { context.startForegroundService(new Intent(context,ControlService.class).setAction("restore")); }
        catch(RuntimeException e) {
            String message="系统阻止自动恢复，请打开App启动控制："+e.getClass().getSimpleName();
            prefs.edit().putString("recovery_error",message).apply();ControlService.status=message;
            android.util.Log.w("PhoneRelay",message,e);
        }
    }
}
