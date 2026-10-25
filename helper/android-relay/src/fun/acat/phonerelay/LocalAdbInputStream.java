package fun.acat.phonerelay;

import io.github.muntashirakon.adb.AdbStream;
import java.io.IOException;
import java.io.InputStream;

/** 先读取 ADB 已接收的缓冲数据，由协议退出码确认命令完成。 */
final class LocalAdbInputStream extends InputStream {
    private final AdbStream stream;

    LocalAdbInputStream(AdbStream stream) { this.stream = stream; }

    @Override public int read() throws IOException {
        byte[] value = new byte[1];
        int count = read(value, 0, 1);
        return count < 0 ? -1 : value[0] & 255;
    }

    @Override public int read(byte[] value, int offset, int length) throws IOException {
        if (offset < 0 || length < 0 || offset > value.length - length)
            throw new IndexOutOfBoundsException();
        if (length == 0) return 0;
        return stream.read(value, offset, length);
    }
}
