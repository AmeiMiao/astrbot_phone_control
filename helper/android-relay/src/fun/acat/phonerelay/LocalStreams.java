package fun.acat.phonerelay;

import io.github.muntashirakon.adb.AdbStream;
import java.io.*;
import java.net.Socket;
import java.util.concurrent.*;

/** 数据子连接绑定本次控制会话，关闭时结束对应本机ADB流。 */
final class LocalStreams implements AutoCloseable {
    interface Dial { Socket open(String id) throws Exception; }
    interface Artifact { InputStream open(String id) throws Exception; }
    interface Progress { void report(String id,long received) throws IOException; }
    private final Progress progress;
    private final LocalAdb adb;
    private final Dial dial;
    private final Artifact artifacts;
    private final ConcurrentHashMap<String, Channel> channels = new ConcurrentHashMap<>();
    private volatile boolean closed;
    private final Semaphore slots = new Semaphore(8);
    LocalStreams(LocalAdb adb, Dial dial, Artifact artifacts,Progress progress) { this.adb = adb; this.dial = dial; this.artifacts = artifacts;this.progress=progress; }
    private final class Channel implements AutoCloseable {
        final String request, id;
        Socket peer;
        AdbStream stream;
        InputStream artifact;
        boolean ended;
        Channel(String request, String id) { this.request = request; this.id = id; }
        synchronized void peer(Socket value) throws IOException { if (ended) { value.close(); throw new IOException("通道已取消"); } peer = value; }
        synchronized void artifact(InputStream value) throws IOException { if(ended){value.close();throw new IOException("通道已取消");}artifact=value; }
        synchronized void stream(AdbStream value) throws IOException { if (ended) { value.close(); throw new IOException("通道已取消"); } stream = value; }
        public synchronized void close() {
            if (ended) return;
            ended = true;
            channels.remove(id, this); slots.release();
            if (peer != null) try { peer.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭数据连接失败", e); }
            if (artifact != null) try { artifact.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭录制文件失败", e); }
            if (stream != null) try { stream.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭数据ADB流失败", e); }
        }
    }
    void open(String request, String id, String service,boolean flowControl,boolean frameAck) throws Exception {
        if(frameAck && !service.equals("frames:"))throw new IOException("视频接收确认仅用于视频帧通道");
        if(flowControl && !service.equals("sync:"))throw new IOException("进度确认仅用于文件通道");
        if (closed || !id.matches("[0-9a-f]{32}") || channels.size() >= 8
            || !(service.equals("sync:") || service.equals("frames:") || service.matches("localabstract:scrcpy_[0-9a-f]{8}") || service.matches("artifact:[0-9a-f]{32}"))) throw new IOException("手机数据通道参数或并发无效");
        if (!slots.tryAcquire()) throw new IOException("手机数据通道并发已满");
        Channel channel = new Channel(request, id);
        if (channels.putIfAbsent(id, channel) != null) { channel.close(); throw new IOException("重复数据通道"); }
        try {
            if (service.startsWith("artifact:") || service.equals("frames:")) {
                channel.artifact(service.equals("frames:") ? new LocalFrameStream(adb) : artifacts.open(service.substring(9)));
                channel.peer(dial.open(id)); channel.peer.setSoTimeout(frameAck?15000:0);
                if(frameAck)((LocalFrameStream)channel.artifact).acknowledgements(channel.peer.getInputStream());
                Thread download = new Thread(() -> {
                    try (InputStream input = channel.artifact) { copy(input, channel.peer.getOutputStream()); }
                    catch (IOException e) { if (!channel.ended) android.util.Log.w("PhoneRelay", "录制文件下载结束", e); }
                    finally { channel.close(); }
                }, "relay-artifact-download"); download.setDaemon(true); download.start(); return;
            }
            channel.stream(LocalAdb.bounded("打开手机数据流", 5000, () -> adb.openStream(service)));
            channel.peer(dial.open(id)); channel.peer.setSoTimeout(0);
            Thread upload = new Thread(() -> {
                try {
                    InputStream input=channel.peer.getInputStream();OutputStream output=channel.stream.openOutputStream();
                    byte[] buffer=new byte[65536];int count;long received=0;
                    while((count=input.read(buffer))>=0) {
                        if(count==0)continue;
                        output.write(buffer,0,count);output.flush();received+=count;
                        if(received>Integer.MAX_VALUE)throw new IOException("文件数据通道超过字节上限");
                        if(flowControl)progress.report(id,received);
                    }
                }
                catch (IOException e) { if (!channel.ended) android.util.Log.d("PhoneRelay", "数据上传结束：" + e.getMessage()); }
                finally { channel.close(); }
            }, "relay-data-upload"); upload.setDaemon(true);
            Thread download = new Thread(() -> {
                try { copy(new LocalAdbInputStream(channel.stream), channel.peer.getOutputStream()); }
                catch (IOException e) { if (!channel.ended) android.util.Log.d("PhoneRelay", "数据下载结束：" + e.getMessage()); }
                finally { channel.close(); channels.remove(id, channel); }
            }, "relay-data-download"); download.setDaemon(true);
            upload.start(); download.start();
        } catch (Exception e) { channel.close(); channels.remove(id, channel); throw e; }
    }
    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] bytes = new byte[65536]; int n;
        while ((n = in.read(bytes)) >= 0) { if (n > 0) { out.write(bytes, 0, n); out.flush(); } }
    }
    void cancel(String request) { for (Channel channel : channels.values()) if (channel.request.equals(request)) channel.close(); }
    public void close() { closed = true; for (Channel channel : channels.values()) channel.close(); channels.clear(); }
}
