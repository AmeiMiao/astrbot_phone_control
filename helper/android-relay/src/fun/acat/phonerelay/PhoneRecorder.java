package fun.acat.phonerelay;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.os.SystemClock;
import io.github.muntashirakon.adb.AdbStream;
import java.io.*;
import java.nio.ByteBuffer;
import java.security.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONObject;

/** 手机内将H264与AAC封装为有界MP4，完成后经认证数据连接读取。 */
final class PhoneRecorder implements AutoCloseable {
    private final LocalAdb adb;
    private final File directory;
    private Job current;
    private boolean closed;
    PhoneRecorder(Context context, LocalAdb adb) throws IOException {
        this.adb = adb; directory = new File(context.getCacheDir(), "recordings");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建录制目录");
        File[] previous = directory.listFiles();
        if (previous != null) for (File file : previous)
            if (file.getName().matches("[0-9a-f]{32}\\.mp4") && !file.delete()) throw new IOException("无法清理录制缓存");
    }
    synchronized JSONObject start(JSONObject params) throws Exception {
        if (closed) throw new IOException("录制控制会话已结束");
        if (current != null) throw new IOException("请先下载或取消当前录制");
        String mode = params.getString("mode"), source = params.getString("source");
        double seconds = params.getDouble("seconds");
        if (!(mode.equals("audio") || mode.equals("video") || mode.equals("both"))
            || !(source.equals("output") || source.equals("mic")) || !Double.isFinite(seconds) || seconds < 1 || seconds > 900)
            throw new IOException("录制模式、音源或时长无效");
        String id = params.getString("recording_id");
        if (!id.matches("[0-9a-f]{32}")) throw new IOException("录制身份无效");
        current = new Job(id, mode, source, seconds);
        try { current.start(); return current.info(); }
        catch (Exception e) { current.finish("error"); if (current.file.exists() && !current.file.delete()) android.util.Log.w("PhoneRelay", "清理启动失败文件失败"); current = null; throw e; }
    }
    private synchronized Job owned(String id) throws IOException {
        if (current == null || !current.id.equals(id)) throw new IOException("录制身份无效");
        return current;
    }
    JSONObject status(String id) throws Exception { return owned(id).info(); }
    JSONObject stop(String id) throws Exception { Job job = owned(id); job.finish("requested"); return job.info(); }
    synchronized void delete(String id) throws Exception {
        if (current == null) return;
        Job job = owned(id); job.finish("cancelled");
        if (job.readers != 0) throw new IOException("录制文件仍在传输");
        if (job.file.exists() && !job.file.delete()) throw new IOException("无法清理录制文件");
        current = null;
    }
    synchronized InputStream artifact(String id) throws Exception {
        Job job = owned(id);
        synchronized (job) {
            if (!job.finished || !job.error.isEmpty() || !job.muxed || !job.file.isFile()) throw new IOException("录制文件尚未就绪");
            InputStream input = new FileInputStream(job.file); job.readers++;
            return new FilterInputStream(input) {
                final AtomicBoolean ended = new AtomicBoolean();
                public void close() throws IOException {
                    if (!ended.compareAndSet(false, true)) return;
                    try { super.close(); } finally { synchronized (PhoneRecorder.this) { job.readers--; } }
                }
            };
        }
    }
    public synchronized void close() {
        closed=true;
        if (current == null) return;
        current.finish("connection_closed");
        if (current.file.exists() && !current.file.delete()) android.util.Log.w("PhoneRelay", "清理断线录制文件失败");
        current = null;
    }
    private static final class Sample {
        final boolean video; final ScrcpyPackets packet; final long pts;
        Sample(boolean video, ScrcpyPackets packet) { this.video = video; this.packet = packet; pts = packet.flags & ((1L<<61)-1); }
    }
    private final class Job {
        final String id, mode, source;
        final boolean video, audio;
        final double maximum;
        final File file;
        final List<AdbStream> streams = new ArrayList<>();
        final List<Thread> workers = new ArrayList<>();
        final List<Sample> pending = new ArrayList<>();
        final H264Config avc = new H264Config();
        MediaMuxer muxer;
        MediaFormat videoFormat, audioFormat;
        int width, height, videoTrack = -1, audioTrack = -1, readers;
        long videoSamples, audioSamples, bytes, pendingBytes, base = -1, lastVideo = -1, lastAudio = -1, startedAt;
        volatile boolean stopping, finished, muxed;
        volatile String error = "", reason = "";
        String hash = "";
        Job(String id, String mode, String source, double maximum) { this.id = id; this.file = new File(directory, id + ".mp4"); this.mode = mode; this.source = source; this.maximum = maximum; video = !mode.equals("audio"); audio = !mode.equals("video"); }
        void start() throws Exception {
            adb.pushControlServer(); muxer = new MediaMuxer(file.getPath(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            int scid = new SecureRandom().nextInt() & 0x7fffffff;
            String address = String.format(Locale.ROOT, "localabstract:scrcpy_%08x", scid);
            String command = "shell:CLASSPATH=/data/local/tmp/acat-phone-relay-scrcpy.jar app_process / com.genymobile.scrcpy.Server 5.0.1"
                + " log_level=error control=false video=" + video + " audio=" + audio
                + " video_codec=h264 video_bit_rate=2000000 max_size=1280 max_fps=30 audio_codec=aac audio_bit_rate=128000 audio_source=" + source
                + " send_device_meta=false send_stream_meta=true send_frame_meta=true send_dummy_byte=true tunnel_forward=true cleanup=false scid=" + Integer.toHexString(scid);
            streams.add(LocalAdb.bounded("启动手机录制", 5000, () -> adb.openStream(command)));
            AdbStream picture = video ? connect(address) : null;
            AdbStream sound = audio ? connect(address) : null;
            if (picture != null) worker(picture, true, true);
            if (sound != null) worker(sound, false, !video);
            Thread monitor = new Thread(() -> {
                while (!stopping) {
                    if (!error.isEmpty()) { finish("error"); return; }
                    if (startedAt != 0 && SystemClock.elapsedRealtime() - startedAt >= maximum * 1000) { finish("time_limit"); return; }
                    if (bytes >= 124L * 1024 * 1024) { finish("size_limit"); return; }
                    try { Thread.sleep(100); } catch (InterruptedException e) { return; }
                }
            }, "relay-record-limit"); monitor.setDaemon(true); monitor.start();
            synchronized (this) {
                long deadline = SystemClock.elapsedRealtime() + 15000;
                while (!muxed && error.isEmpty() && !finished) {
                    long remaining = deadline - SystemClock.elapsedRealtime();
                    if (remaining <= 0) throw new IOException("手机录制等待音视频轨道超时");
                    wait(remaining);
                }
                if (!error.isEmpty() || finished) throw new IOException(error.isEmpty() ? "手机录制已停止" : error);
            }
        }
        AdbStream connect(String address) throws Exception {
            long deadline = SystemClock.elapsedRealtime() + 5000;
            while (!stopping) {
                try {
                    AdbStream stream = LocalAdb.bounded("打开录制通道", 1000, () -> adb.openStream(address)); streams.add(stream); return stream;
                } catch (IOException e) { if (SystemClock.elapsedRealtime() >= deadline) throw e; Thread.sleep(50); }
            }
            throw new IOException("录制已取消");
        }
        void worker(AdbStream stream, boolean picture, boolean dummy) {
            Thread worker = new Thread(() -> {
                try {
                    DataInputStream input = new DataInputStream(new LocalAdbInputStream(stream));
                    if (dummy && input.readUnsignedByte() != 0) throw new IOException("录制握手无效");
                    if (input.readInt() != (picture ? 0x68323634 : 0x00616163)) throw new IOException(picture ? "手机H264录制不可用" : "手机AAC音频采集不可用");
                    while (!stopping) sample(picture, ScrcpyPackets.read(input));
                } catch (Exception e) {
                    if (!stopping) synchronized (this) { error = "手机录制：" + e.getClass().getSimpleName() + " " + e.getMessage(); notifyAll(); }
                }
            }, picture ? "relay-record-video" : "relay-record-audio"); worker.setDaemon(true); workers.add(worker); worker.start();
        }
        synchronized void sample(boolean picture, ScrcpyPackets packet) throws Exception {
            if (stopping) return;
            if (packet.data == null) {
                if (!picture || muxed && (width != packet.width || height != packet.height)) throw new IOException("录制期间屏幕尺寸变化，请重新录制");
                width = packet.width; height = packet.height; return;
            }
            if ((packet.flags & ScrcpyPackets.CONFIG) != 0) {
                if (picture) {
                    avc.accept(packet.data);
                    if (avc.sps != null && avc.pps != null) {
                        videoFormat = MediaFormat.createVideoFormat("video/avc", width, height);
                        videoFormat.setByteBuffer("csd-0", ByteBuffer.wrap(avc.sps)); videoFormat.setByteBuffer("csd-1", ByteBuffer.wrap(avc.pps));
                    }
                } else {
                    if (packet.data.length < 2 || packet.data.length > 4096) throw new IOException("AAC配置长度无效");
                    audioFormat = MediaFormat.createAudioFormat("audio/mp4a-latm", 48000, 2);
                    audioFormat.setByteBuffer("csd-0", ByteBuffer.wrap(packet.data));
                }
                return;
            }
            Sample sample = new Sample(picture, packet);
            if (base < 0) {
                pending.add(sample); pendingBytes += packet.data.length;
                if (pendingBytes > 2 * 1024 * 1024) throw new IOException("录制轨道启动缓冲已满");
                boolean hasVideo = !video, hasAudio = !audio;
                for (Sample item : pending) { if (item.video) hasVideo = true; else hasAudio = true; }
                if ((!video || videoFormat != null) && (!audio || audioFormat != null) && hasVideo && hasAudio) {
                    if (video) videoTrack = muxer.addTrack(videoFormat);
                    if (audio) audioTrack = muxer.addTrack(audioFormat);
                    muxer.start(); muxed = true; startedAt = SystemClock.elapsedRealtime();
                    pending.sort((a,b) -> Long.compare(a.pts,b.pts)); base = pending.get(0).pts;
                    for (Sample item : pending) write(item);
                    pending.clear(); pendingBytes = 0; notifyAll();
                }
            } else write(sample);
        }
        void write(Sample sample) throws Exception {
            long timestamp = sample.pts - base;
            if (timestamp < 0 || timestamp < (sample.video ? lastVideo : lastAudio)) throw new IOException("录制时间戳乱序");
            if (bytes + sample.packet.data.length > 128L * 1024 * 1024) throw new IOException("录制超过128 MiB上限");
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            info.set(0, sample.packet.data.length, timestamp, (sample.packet.flags & ScrcpyPackets.KEY) != 0 ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0);
            muxer.writeSampleData(sample.video ? videoTrack : audioTrack, ByteBuffer.wrap(sample.packet.data), info);
            bytes += sample.packet.data.length;
            if (sample.video) { lastVideo = timestamp; videoSamples++; } else { lastAudio = timestamp; audioSamples++; }
        }
        void finish(String why) {
            synchronized (this) {
                if (finished) return;
                if (stopping) {
                    long deadline = SystemClock.elapsedRealtime() + 10000;
                    while (!finished && SystemClock.elapsedRealtime() < deadline) try { wait(100); } catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
                    return;
                }
                stopping = true; reason = why;
            }
            for (AdbStream stream : streams) try { stream.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭录制流失败", e); }
            for (Thread worker : workers) if (worker != Thread.currentThread()) try { worker.join(2000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            synchronized (this) {
                if (muxer != null) {
                    try { if (muxed) muxer.stop(); } catch (RuntimeException e) { error = "手机MP4封装失败：" + e.getMessage(); }
                    try { muxer.release(); } catch (RuntimeException e) { error = "释放手机MP4封装失败：" + e.getMessage(); }
                    muxer = null;
                }
                if (!muxed && error.isEmpty()) error = "录制未产生有效轨道";
                if (error.isEmpty()) {
                    try {
                        MessageDigest digest = MessageDigest.getInstance("SHA-256");
                        try (InputStream input = new FileInputStream(file)) { byte[] chunk = new byte[65536]; int n; while ((n=input.read(chunk))>=0) digest.update(chunk,0,n); }
                        hash = ControlService.hex(digest.digest());
                    } catch (Exception e) { error = "录制文件校验失败：" + e.getMessage(); }
                }
                if (!error.isEmpty() && file.exists() && !file.delete()) android.util.Log.w("PhoneRelay", "清理失败录制文件失败");
                finished = true; notifyAll();
            }
        }
        synchronized JSONObject info() throws Exception {
            return new JSONObject().put("recording_id", id).put("running", !finished && !stopping).put("ready", finished && error.isEmpty())
                .put("mode", mode).put("seconds", Math.max(lastVideo,lastAudio) / 1000000.0).put("size", finished && file.isFile() ? file.length() : bytes)
                .put("sha256", hash).put("video_samples", videoSamples).put("audio_samples", audioSamples).put("reason", reason).put("error", error);
        }
    }
}
