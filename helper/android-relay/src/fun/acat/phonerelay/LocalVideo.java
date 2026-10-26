package fun.acat.phonerelay;

import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.media.Image;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.SystemClock;
import android.util.Base64;
import io.github.muntashirakon.adb.AdbStream;
import java.io.*;
import java.nio.ByteBuffer;
import java.security.SecureRandom;
import java.util.Locale;
import org.json.JSONObject;

/** 手机内解码 scrcpy 视频，按需回传下一张实际帧，空闲后释放编码器。 */
final class LocalVideo implements AutoCloseable {
    private final LocalAdb adb;
    private volatile AdbStream stream, server;
    private volatile boolean running;
    volatile boolean closed;
    private Thread worker;
    private Frame frame;
    private volatile boolean keepAlive;
    private volatile long requiredAfter;
    private static final class Frame {
        final byte[] pixels; final int width, height; final long at;
        volatile byte[] encoded;
        Frame(byte[] pixels, int width, int height, long capturedAt) {
            this.pixels = pixels; this.width = width; this.height = height;
            at = capturedAt;
        }
        synchronized byte[] jpeg() throws IOException {
            if (encoded == null) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                if (!new YuvImage(pixels, ImageFormat.NV21, width, height, null)
                    .compressToJpeg(new Rect(0, 0, width, height), 85, out)) throw new IOException("视频帧编码失败");
                encoded = out.toByteArray();
            }
            return encoded;
        }
    }
    private volatile long requestedAt;
    private String error;
    private volatile String stage = "启动";

    LocalVideo(LocalAdb adb) { this.adb = adb; }

    synchronized void standby(boolean enabled) {
        keepAlive = enabled;
        if (enabled) start();
    }
    void invalidate() { requiredAfter = SystemClock.elapsedRealtime(); }
    private synchronized void start() {
        if (running || closed) return;
        requestedAt = SystemClock.elapsedRealtime();
        frame = null; error = null; running = true;
        worker = new Thread(this::decode, "relay-video"); worker.start();
        Thread idle = new Thread(() -> {
            while (running) {
                try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
                if (!keepAlive && SystemClock.elapsedRealtime() - requestedAt > 90000) close();
            }
        }, "relay-video-idle"); idle.setDaemon(true); idle.start();
    }
    JSONObject frame() throws Exception {
        Frame selected;
        synchronized (this) {
            if (closed) throw new IOException("手机视频通道已关闭");
            requestedAt = SystemClock.elapsedRealtime();
            long after = Math.max(requestedAt - 80, requiredAfter);
            start();
            long deadline = requestedAt + 15000;
            while (running && error == null && (frame == null || frame.at < after)) {
                long remaining = deadline - SystemClock.elapsedRealtime();
                if (remaining <= 0) { close(); throw new IOException("手机 scrcpy 等待新视频帧超时：" + stage + " / " + adb.componentStage + " / " + (worker == null ? "none" : worker.getState())); }
                wait(remaining);
            }
            if (error != null || !running) throw new IOException(error == null ? "手机视频已停止" : error);
            selected = frame;
        }
        long compressStarted = SystemClock.elapsedRealtime();
        byte[] encoded = selected.jpeg();
        return new JSONObject().put("image", Base64.encodeToString(encoded, Base64.NO_WRAP))
            .put("format", "jpeg").put("source", "app_scrcpy").put("age_ms", SystemClock.elapsedRealtime() - selected.at)
            .put("jpeg_ms", SystemClock.elapsedRealtime() - compressStarted);
    }

    private void decode() {
        Decoder decoder = null;
        try {
            stage = "写入组件"; adb.pushControlServer();
            stage = "启动服务";
            int scid = new SecureRandom().nextInt() & 0x7fffffff;
            String command = "shell:CLASSPATH=/data/local/tmp/acat-phone-relay-scrcpy.jar app_process / com.genymobile.scrcpy.Server 5.0.1"
                + " log_level=error video=true audio=false control=false video_codec=h264 max_fps=30 video_bit_rate=4000000"
                + " send_device_meta=false send_stream_meta=true send_frame_meta=true send_dummy_byte=true tunnel_forward=true cleanup=false scid="
                + Integer.toHexString(scid);
            server = LocalAdb.bounded("启动视频组件", 5000, () -> adb.openStream(command));
            stage = "打开视频流";
            String address = String.format(Locale.ROOT, "localabstract:scrcpy_%08x", scid);
            long deadline = SystemClock.elapsedRealtime() + 5000;
            while (running && stream == null && SystemClock.elapsedRealtime() < deadline) {
                try { stream = LocalAdb.bounded("打开视频通道", 1000, () -> adb.openStream(address)); }
                catch (IOException e) { Thread.sleep(100); }
            }
            if (stream == null || !running) throw new IOException("手机视频通道未就绪");
            stage = "视频握手";
            DataInputStream in = new DataInputStream(new LocalAdbInputStream(stream));
            LocalAdb.bounded("视频握手", 5000, () -> {
                if (in.readUnsignedByte() != 0 || in.readInt() != 0x68323634) throw new IOException("视频协议无效");
                return null;
            });
            while (running) {
                stage = "读取视频包";
                ScrcpyPackets packet = ScrcpyPackets.read(in);
                if (packet.data == null) {
                    if (decoder != null) decoder.close();
                    synchronized (this) { frame = null; }
                    decoder = new Decoder(packet.width, packet.height);
                } else {
                    if (decoder == null) throw new IOException("视频缺少会话尺寸");
                    decoder.input(packet);
                }
            }
        } catch (Exception e) {
            synchronized (this) { error = "手机 scrcpy：" + e.getClass().getSimpleName() + " " + e.getMessage()
                + (e instanceof MediaCodec.CodecException ? " " + ((MediaCodec.CodecException)e).getDiagnosticInfo() : ""); notifyAll(); }
        } finally {
            if (decoder != null) decoder.close();
            close();
        }
    }

    private final class Decoder implements AutoCloseable {
        final MediaCodec codec;
        final Thread output;
        volatile boolean active = true;
        Decoder(int width, int height) throws Exception {
            stage = "建立解码器";
            codec = MediaCodec.createDecoderByType("video/avc");
            try {
                MediaFormat format = MediaFormat.createVideoFormat("video/avc", width, height);
                format.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
                format.setInteger(MediaFormat.KEY_PRIORITY, 0);
                if (codec.getCodecInfo().getCapabilitiesForType("video/avc").isFeatureSupported("low-latency"))
                    format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
                codec.configure(format, null, null, 0); codec.start();
            } catch (Exception e) { codec.release(); throw e; }
            output = new Thread(() -> {
                MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
                try {
                    while (active && running) {
                        int index = codec.dequeueOutputBuffer(info, 10000);
                        if (index < 0) continue;
                        try (Image image = codec.getOutputImage(index)) {
                            if (image != null) {
                                Frame decoded = pixels(image, info.presentationTimeUs);
                                synchronized (LocalVideo.this) { frame = decoded; LocalVideo.this.notifyAll(); }
                            }
                        } finally { codec.releaseOutputBuffer(index, false); }
                    }
                } catch (Exception e) {
                    if (active && running) {
                        synchronized (LocalVideo.this) { error = "手机视频解码失败：" + e.getMessage(); LocalVideo.this.notifyAll(); }
                        LocalVideo.this.close();
                    }
                }
            }, "relay-video-output"); output.setDaemon(true); output.start();
        }
        void input(ScrcpyPackets packet) throws Exception {
            int index = codec.dequeueInputBuffer(100000);
            if (index < 0) throw new IOException("手机视频解码输入阻塞");
            ByteBuffer buffer = codec.getInputBuffer(index);
            if (buffer == null || packet.data.length > buffer.capacity()) throw new IOException("视频解码缓冲不足");
            buffer.clear(); buffer.put(packet.data);
            codec.queueInputBuffer(index, 0, packet.data.length, packet.flags & ((1L << 61) - 1),
                (packet.flags & ScrcpyPackets.CONFIG) != 0 ? MediaCodec.BUFFER_FLAG_CODEC_CONFIG : 0);
        }
        public void close() {
            active = false;
            try { output.join(1000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            try { codec.stop(); } catch (IllegalStateException e) { android.util.Log.w("PhoneRelay", "停止视频解码器失败", e); }
            codec.release();
        }
    }

    private static Frame pixels(Image image, long presentationUs) throws IOException {
        Rect crop = image.getCropRect();
        Image.Plane[] planes = image.getPlanes();
        ByteBuffer[] data = new ByteBuffer[3]; int[] rows = new int[3], strides = new int[3];
        for (int i = 0; i < 3; i++) { data[i] = planes[i].getBuffer(); rows[i] = planes[i].getRowStride(); strides[i] = planes[i].getPixelStride(); }
        return new Frame(YuvPlanes.nv21(data, rows, strides, crop.left, crop.top, crop.width(), crop.height()), crop.width(), crop.height(), SystemClock.elapsedRealtime() - (System.nanoTime() / 1000000 - presentationUs / 1000));
    }

    public void close() {
        closed = true;
        running = false;
        for (AdbStream s : new AdbStream[]{stream, server}) if (s != null)
            try { s.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭视频失败", e); }
        stream = null; server = null;
        synchronized (this) { frame = null; notifyAll(); }
    }
}
