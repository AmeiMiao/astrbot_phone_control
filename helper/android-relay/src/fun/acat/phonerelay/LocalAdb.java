package fun.acat.phonerelay;

import android.content.Context;
import android.os.Build;
import android.util.Base64;
import io.github.muntashirakon.adb.AbsAdbConnectionManager;
import io.github.muntashirakon.adb.AdbStream;
import java.io.*;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.*;
import java.security.cert.*;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.*;
import java.util.concurrent.*;
import javax.security.auth.x500.X500Principal;
import org.bouncycastle.x509.X509V3CertificateGenerator;
import org.conscrypt.Conscrypt;
import org.json.JSONObject;

/** 私有 ADB 身份及本机 adbd 的 shell、控制通道。 */
final class LocalAdb extends AbsAdbConnectionManager {
    private final PrivateKey key;
    private final java.security.cert.Certificate certificate;
    private final Context context;
    private final ConcurrentHashMap<String, AdbStream> active = new ConcurrentHashMap<>();
    private AdbStream control;
    private AdbStream server;
    private DataInputStream controlIn;
    private DataOutputStream controlOut;
    private long sequence;

    LocalAdb(Context context) throws Exception {
        this.context = context;
        Security.insertProviderAt(Conscrypt.newProvider(), 1);
        File keyFile = new File(context.getFilesDir(), "adb-key.pk8");
        File certFile = new File(context.getFilesDir(), "adb-cert.der");
        if (keyFile.isFile() && certFile.isFile()) {
            key = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(readFile(keyFile)));
            certificate = CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(readFile(certFile)));
        } else {
            KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
            generator.initialize(2048);
            KeyPair pair = generator.generateKeyPair();
            key = pair.getPrivate();
            X509V3CertificateGenerator builder = new X509V3CertificateGenerator();
            builder.setSerialNumber(new BigInteger(128, new SecureRandom()).abs());
            builder.setIssuerDN(new X500Principal("CN=Phone Relay"));
            builder.setSubjectDN(new X500Principal("CN=Phone Relay"));
            builder.setNotBefore(new Date(System.currentTimeMillis() - 86400000L));
            builder.setNotAfter(new Date(System.currentTimeMillis() + 315360000000L));
            builder.setPublicKey(pair.getPublic());
            builder.setSignatureAlgorithm("SHA256WithRSA");
            certificate = builder.generate(key);
            writeFile(keyFile, key.getEncoded());
            writeFile(certFile, certificate.getEncoded());
        }
        setApi(Build.VERSION.SDK_INT);
        setTimeout(15, TimeUnit.SECONDS);
    }
    protected PrivateKey getPrivateKey() { return key; }
    protected java.security.cert.Certificate getCertificate() { return certificate; }
    protected String getDeviceName() { return "PhoneRelay"; }

    static byte[] readFile(File file) throws IOException {
        try (InputStream in = new FileInputStream(file)) { return readBounded(in, 32 * 1024 * 1024); }
    }
    static void writeFile(File file, byte[] bytes) throws IOException {
        try (OutputStream out = new FileOutputStream(file)) { out.write(bytes); }
    }
    static byte[] readBounded(InputStream in, int limit) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[16384]; int n;
        while ((n = in.read(buffer)) != -1) {
            if (out.size() + n > limit) throw new IOException("输出超过上限");
            out.write(buffer, 0, n);
        }
        return out.toByteArray();
    }
    static void exact(InputStream in, byte[] bytes) throws IOException {
        int at = 0, n;
        while (at < bytes.length) {
            n = in.read(bytes, at, bytes.length - at);
            if (n < 0) throw new EOFException("ADB 数据不完整");
            at += n;
        }
    }

    JSONObject shell(String id, String command, int limit) throws Exception {
        AdbStream stream = openStream("shell,v2,raw:" + command);
        active.put(id, stream);
        try {
            InputStream in = stream.openInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream(), err = new ByteArrayOutputStream();
            while (true) {
                int kind = in.read();
                if (kind < 0) throw new EOFException("ADB 未返回退出码");
                byte[] header = new byte[4]; exact(in, header);
                int size = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN).getInt();
                if (size < 0 || size > 8 * 1024 * 1024 || out.size() + err.size() + size > limit)
                    throw new IOException("ADB 输出超过上限");
                byte[] data = new byte[size]; exact(in, data);
                if (kind == 1) out.write(data);
                else if (kind == 2) err.write(data);
                else if (kind == 3) {
                    if (size != 1) throw new IOException("ADB 退出码格式无效");
                    return new JSONObject().put("rc", data[0] & 255)
                        .put("stdout", Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP))
                        .put("stderr", Base64.encodeToString(err.toByteArray(), Base64.NO_WRAP));
                } else throw new IOException("ADB shell 消息无效");
            }
        } finally { active.remove(id); stream.close(); }
    }
    JSONObject nodes(String id, int limit) throws Exception {
        String file = "/data/local/tmp/acat-relay-ui-" + id + ".xml";
        return shell(id, "trap 'rm -f " + file + "' EXIT; uiautomator dump " + file + " >/dev/null && cat " + file, limit);
    }
    void cancel(String id) {
        AdbStream stream = active.remove(id);
        if (stream != null) try { stream.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "取消 ADB 流失败", e); }
        closeControl();
    }
    private void syncPacket(OutputStream out, String tag, byte[] bytes, int length) throws IOException {
        out.write(tag.getBytes("US-ASCII"));
        out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(length).array());
        if (bytes != null) out.write(bytes);
        out.flush();
    }
    private void pushControlServer() throws Exception {
        String path = "/data/local/tmp/acat-phone-relay-scrcpy.jar";
        try (AdbStream sync = openStream("sync:"); InputStream jar = context.getAssets().open("scrcpy-server")) {
            OutputStream out = sync.openOutputStream();
            byte[] name = (path + ",33188").getBytes("UTF-8");
            syncPacket(out, "SEND", name, name.length);
            byte[] buf = new byte[65536]; int n;
            while ((n = jar.read(buf)) > 0) syncPacket(out, "DATA", Arrays.copyOf(buf, n), n);
            syncPacket(out, "DONE", null, (int)(System.currentTimeMillis() / 1000));
            byte[] response = new byte[8]; exact(sync.openInputStream(), response);
            if (!new String(response, 0, 4, "US-ASCII").equals("OKAY")) throw new IOException("控制组件写入失败");
        }
    }
    private void ensureControl() throws Exception {
        if (control != null && !control.isClosed()) return;
        closeControl();
        pushControlServer();
        int scid = new SecureRandom().nextInt() & 0x7fffffff;
        server = openStream("shell:CLASSPATH=/data/local/tmp/acat-phone-relay-scrcpy.jar app_process / com.genymobile.scrcpy.Server 5.0.1 log_level=error video=false audio=false control=true clipboard_autosync=false raw_stream=true send_dummy_byte=true tunnel_forward=true cleanup=false scid=" + Integer.toHexString(scid));
        String address = String.format(Locale.ROOT, "localabstract:scrcpy_%08x", scid);
        Exception failure = null;
        for (int i = 0; i < 40; i++) {
            try { control = openStream(address); break; }
            catch (IOException e) { failure = e; Thread.sleep(100); }
        }
        if (control == null) throw new IOException("手机控制通道未就绪", failure);
        controlIn = new DataInputStream(control.openInputStream());
        controlOut = new DataOutputStream(control.openOutputStream());
        if (controlIn.read() != 0) throw new IOException("控制通道握手失败");
    }
    synchronized JSONObject clipboard() throws Exception {
        ensureControl();
        controlOut.write(new byte[]{8, 0}); controlOut.flush();
        int kind = controlIn.readUnsignedByte();
        if (kind != 0) throw new IOException("剪贴板回应无效");
        int size = controlIn.readInt();
        if (size < 0 || size > 262139) throw new IOException("剪贴板过大");
        byte[] data = new byte[size]; controlIn.readFully(data);
        return new JSONObject().put("text", new String(data, "UTF-8"));
    }
    synchronized JSONObject input(String text, boolean enter) throws Exception {
        ensureControl();
        byte[] data = text.getBytes("UTF-8");
        if (data.length > 262000) throw new IOException("输入文本过大");
        controlOut.writeByte(9); controlOut.writeLong(++sequence); controlOut.writeBoolean(true);
        controlOut.writeInt(data.length); controlOut.write(data); controlOut.flush();
        if (controlIn.readUnsignedByte() != 1 || controlIn.readLong() != sequence)
            throw new IOException("输入确认无效；本次不重试");
        if (enter) shell("enter", "input keyevent 66", 8192);
        return new JSONObject();
    }
    private void closeControl() {
        AdbStream c = control, s = server;
        control = null; server = null;
        for (AdbStream stream : new AdbStream[]{c, s}) if (stream != null)
            try { stream.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭控制通道失败", e); }
    }
    @Override public void close() throws IOException {
        for (String id : active.keySet()) cancel(id);
        closeControl(); super.close();
    }
}
