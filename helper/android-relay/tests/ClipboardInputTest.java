package fun.acat.phonerelay;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

public final class ClipboardInputTest {
    private static byte[] ack(long sequence) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream out = new DataOutputStream(bytes);
        out.writeByte(1); out.writeLong(sequence);
        return bytes.toByteArray();
    }
    private static void check(boolean value) {
        if (!value) throw new AssertionError();
    }
    public static void main(String[] args) throws Exception {
        String text = "重复输入确认123";
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        int[] pasted = {0};
        for (long sequence = 1; sequence <= 3; sequence++) {
            DataInputStream in = new DataInputStream(new ByteArrayInputStream(ack(sequence)));
            ClipboardInput.input(in, new DataOutputStream(bytes), sequence, text, () -> {
                check(in.available() == 0);
                pasted[0]++;
            });
        }
        check(pasted[0] == 3);
        DataInputStream sent = new DataInputStream(new ByteArrayInputStream(bytes.toByteArray()));
        for (long sequence = 1; sequence <= 3; sequence++) {
            check(sent.readUnsignedByte() == 9 && sent.readLong() == sequence);
            check(!sent.readBoolean());
            byte[] data = new byte[sent.readInt()]; sent.readFully(data);
            check(Arrays.equals(data, text.getBytes(StandardCharsets.UTF_8)));
        }
        check(sent.available() == 0);
        for (byte[] response : new byte[][]{ack(99), new byte[]{0}, new byte[]{1, 0}}) {
            try {
                ClipboardInput.input(new DataInputStream(new ByteArrayInputStream(response)),
                    new DataOutputStream(new ByteArrayOutputStream()), 4, text, () -> pasted[0]++);
                throw new AssertionError("无效确认应终止输入");
            } catch (IOException expected) { check(pasted[0] == 3); }
        }
        int[] attempts = {0};
        try {
            ClipboardInput.input(new DataInputStream(new ByteArrayInputStream(ack(5))),
                new DataOutputStream(new ByteArrayOutputStream()), 5, text, () -> {
                    attempts[0]++;
                    throw new IOException("粘贴执行失败");
                });
            throw new AssertionError("执行失败应传播");
        } catch (IOException expected) { check(attempts[0] == 1); }
        System.out.println("ClipboardInput: repeated text, single paste, ACK ordering and failure propagation passed");
    }
}
