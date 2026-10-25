package fun.acat.phonerelay;

import io.github.muntashirakon.adb.AdbStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.concurrent.ConcurrentLinkedQueue;

/** 在实际依赖上复现远端关闭时，队列已空而读缓冲仍有数据的状态。 */
public final class LocalAdbInputStreamTest {
    private static void set(AdbStream stream, String name, Object value) throws Exception {
        Field field = AdbStream.class.getDeclaredField(name);
        field.setAccessible(true); field.set(stream, value);
    }

    public static void main(String[] args) throws Exception {
        // 仅在宿主测试中构造流状态，无需运行 Android 或连接设备。
        Class<?> type = Class.forName("sun.misc.Unsafe");
        Field singleton = type.getDeclaredField("theUnsafe"); singleton.setAccessible(true);
        AdbStream stream = (AdbStream) type.getMethod("allocateInstance", Class.class)
            .invoke(singleton.get(null), AdbStream.class);
        set(stream, "mReadQueue", new ConcurrentLinkedQueue<byte[]>());
        set(stream, "mReadBuffer", ByteBuffer.allocate(32).flip());
        Method payload = AdbStream.class.getDeclaredMethod("addPayload", byte[].class);
        payload.setAccessible(true); payload.invoke(stream, new byte[]{1, 2, 3, (byte)255});
        LocalAdbInputStream input = new LocalAdbInputStream(stream);
        if (input.read() != 1) throw new AssertionError("首字节读取失败");
        Method close = AdbStream.class.getDeclaredMethod("notifyClose", boolean.class);
        close.setAccessible(true); close.invoke(stream, true);
        if (!stream.isClosed() || stream.openInputStream().read() != -1)
            throw new AssertionError("未复现库流提前结束的状态");
        byte[] tail = new byte[2];
        if (input.read(tail) != 2 || !Arrays.equals(tail, new byte[]{2, 3}))
            throw new AssertionError("远端关闭后丢失已接收的尾部数据");
        if (input.read() != 255) throw new AssertionError("单字节读取必须返回无符号值");
        if (input.read(new byte[0]) != 0) throw new AssertionError("空读取必须返回零");
        System.out.println("LocalAdbInputStream: 已关闭流的缓冲尾部、无符号字节及空读取通过");
    }
}
