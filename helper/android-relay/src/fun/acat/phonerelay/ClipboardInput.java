package fun.acat.phonerelay;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** 剪贴板确认后执行一次有完成状态的粘贴。 */
final class ClipboardInput {
    interface Paste { void run() throws Exception; }

    static void input(DataInputStream in, DataOutputStream out, long sequence, String text, Paste paste) throws Exception {
        byte[] data = text.getBytes(StandardCharsets.UTF_8);
        if (data.length > 262000) throw new IOException("输入文本过大");
        out.writeByte(9);
        out.writeLong(sequence);
        out.writeBoolean(false);
        out.writeInt(data.length);
        out.write(data);
        out.flush();
        if (in.readUnsignedByte() != 1 || in.readLong() != sequence)
            throw new IOException("输入确认无效；本次不重试");
        paste.run();
    }
}
