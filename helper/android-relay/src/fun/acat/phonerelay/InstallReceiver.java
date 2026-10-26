package fun.acat.phonerelay;

import android.content.*;
import android.content.pm.PackageInstaller;

/** 系统安装结果及用户确认入口。 */
public final class InstallReceiver extends BroadcastReceiver {
    public static volatile Intent confirmation;
    @Override public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            confirmation = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            ControlService.status = "请点击确认系统安装";
        } else if (status == PackageInstaller.STATUS_SUCCESS) {
            confirmation = null; UpdateInstaller.pending = null;
            if(!ControlService.connected)ControlService.status = "更新成功，请重新启动 App 控制";
            RecoveryReceiver.restoreIfWanted(context,"系统安装完成");
        } else {
            confirmation = null;
            ControlService.status = "安装未完成：" + intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        }
    }
}
