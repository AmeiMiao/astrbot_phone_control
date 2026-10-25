package fun.acat.phonerelay;

import android.app.*;
import android.content.*;
import android.content.pm.*;
import android.os.Build;
import java.io.*;
import java.util.Arrays;

/** 校验同包同签名更新，在可见 Activity 中调用系统安装确认。 */
final class UpdateInstaller {
    static volatile File pending;
    static long version(PackageInfo info) { return Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode; }
    private static Signature[] signatures(PackageInfo info) {
        if (Build.VERSION.SDK_INT >= 28) return info.signingInfo.getApkContentsSigners();
        return info.signatures;
    }
    static void prepare(Context context, File file, int expectedVersion) throws Exception {
        PackageManager pm = context.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo archive = pm.getPackageArchiveInfo(file.getAbsolutePath(), flags);
        PackageInfo installed = pm.getPackageInfo(context.getPackageName(), flags);
        if (archive == null || !context.getPackageName().equals(archive.packageName)
                || version(archive) != expectedVersion || version(archive) <= version(installed)
                || !Arrays.equals(signatures(archive), signatures(installed))) {
            file.delete(); throw new IOException("包名、签名或版本不匹配，拒绝安装");
        }
        pending = file;
    }
    static void install(Activity activity) throws Exception {
        File file = pending;
        if (file == null || !file.isFile()) throw new IOException("请先下载并验证安装包");
        if (!activity.getPackageManager().canRequestPackageInstalls()) {
            activity.startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                android.net.Uri.parse("package:" + activity.getPackageName())));
            return;
        }
        PackageInstaller installer = activity.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(activity.getPackageName()); params.setSize(file.length());
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_REQUIRED);
        int id = installer.createSession(params);
        try (PackageInstaller.Session session = installer.openSession(id);
             InputStream input = new FileInputStream(file);
             OutputStream output = session.openWrite("base.apk", 0, file.length())) {
            byte[] buffer = new byte[65536]; int n;
            while ((n = input.read(buffer)) > 0) output.write(buffer, 0, n);
            session.fsync(output); output.close();
            Intent callback = new Intent(activity, InstallReceiver.class);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
            session.commit(PendingIntent.getBroadcast(activity, id, callback, flags).getIntentSender());
        } catch (Exception e) { installer.abandonSession(id); throw e; }
    }
}
