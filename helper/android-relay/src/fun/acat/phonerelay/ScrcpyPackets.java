package fun.acat.phonerelay;

import java.io.DataInputStream;
import java.io.IOException;

/** scrcpy 5.0.1 视频会话与帧头，所有分配先检查边界。 */
final class ScrcpyPackets {
    static final long SESSION = 1L << 63;
    static final long CONFIG = 1L << 62;
    static final long KEY = 1L << 61;
    final long flags;
    final int width, height;
    final byte[] data;

    private ScrcpyPackets(long flags, int width, int height, byte[] data) {
        this.flags = flags; this.width = width; this.height = height; this.data = data;
    }
    static ScrcpyPackets read(DataInputStream in) throws IOException {
        long flags = in.readLong();
        int size = in.readInt();
        if ((flags & SESSION) != 0) {
            int width = (int) flags;
            if (width <= 0 || size <= 0 || width > 4096 || size > 4096 || (long) width * size > 9000000)
                throw new IOException("视频尺寸超出上限");
            return new ScrcpyPackets(flags, width, size, null);
        }
        if (size <= 0 || size > 4 * 1024 * 1024) throw new IOException("视频帧大小无效");
        byte[] data = new byte[size]; in.readFully(data);
        return new ScrcpyPackets(flags, 0, 0, data);
    }
}
