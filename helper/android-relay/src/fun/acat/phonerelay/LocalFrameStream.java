package fun.acat.phonerelay;

import android.os.SystemClock;
import java.io.*;
import java.nio.ByteBuffer;
import org.json.JSONObject;

/** 独立数据连接以有界频率推送手机采集帧，帧体直接使用JPEG字节。 */
final class LocalFrameStream extends InputStream {
    private final LocalAdb adb;
    private byte[] packet = new byte[0];
    private int position;
    private long next;
    private volatile boolean closed;
    private volatile Thread reader;
    LocalFrameStream(LocalAdb adb) { this.adb = adb; }
    public int read() throws IOException { byte[] byteValue = new byte[1]; return read(byteValue,0,1) < 0 ? -1 : byteValue[0]&255; }
    public int read(byte[] bytes, int offset, int length) throws IOException {
        if (offset < 0 || length < 0 || offset > bytes.length-length) throw new IndexOutOfBoundsException();
        if (length == 0) return 0;
        if (closed) return -1;
        reader = Thread.currentThread();
        if (position == packet.length) {
            try {
                long pause = next-SystemClock.elapsedRealtime();
                if (pause > 0) Thread.sleep(pause);
                if (closed) return -1;
                LocalVideo.FrameImage image = adb.frameImage();
                byte[] header = new JSONObject().put("generation",image.generation)
                    .put("captured_at_ms",image.at).put("age_ms",SystemClock.elapsedRealtime()-image.at).toString().getBytes("UTF-8");
                packet = ByteBuffer.allocate(8+header.length+image.data.length).putInt(header.length).put(header)
                    .putInt(image.data.length).put(image.data).array(); position=0;
                next = SystemClock.elapsedRealtime()+100;
            } catch (Exception e) { throw new IOException("手机视频推流："+e.getMessage(),e); }
        }
        int count=Math.min(length,packet.length-position);System.arraycopy(packet,position,bytes,offset,count);position+=count;return count;
    }
    public void close() { closed=true;Thread active=reader;if(active!=null && active!=Thread.currentThread())active.interrupt(); }
}
