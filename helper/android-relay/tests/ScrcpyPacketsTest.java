package fun.acat.phonerelay;

import java.io.*;

public final class ScrcpyPacketsTest {
    static DataInputStream packet(long flags, int size, byte[] data) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream out = new DataOutputStream(bytes);
        out.writeLong(flags); out.writeInt(size); if (data != null) out.write(data);
        return new DataInputStream(new ByteArrayInputStream(bytes.toByteArray()));
    }
    public static void main(String[] args) throws Exception {
        ScrcpyPackets session = ScrcpyPackets.read(packet(ScrcpyPackets.SESSION | 720, 1280, null));
        if (session.width != 720 || session.height != 1280 || session.data != null) throw new AssertionError();
        ScrcpyPackets rotated = ScrcpyPackets.read(packet(ScrcpyPackets.SESSION | 1280, 720, null));
        if (rotated.width != 1280 || rotated.height != 720) throw new AssertionError();
        ScrcpyPackets frame = ScrcpyPackets.read(packet(ScrcpyPackets.CONFIG, 3, new byte[]{0, 1, 2}));
        if (frame.data.length != 3 || frame.flags != ScrcpyPackets.CONFIG) throw new AssertionError();
        for (DataInputStream invalid : new DataInputStream[]{packet(0, -1, null), packet(0, 5000000, null),
                packet(0, 4, new byte[]{1}), packet(ScrcpyPackets.SESSION | 9999, 1280, null)}) {
            try { ScrcpyPackets.read(invalid); throw new AssertionError("必须拒绝损坏视频包"); }
            catch (IOException expected) { }
        }
        System.out.println("ScrcpyPackets: session, rotation, config, bounds and truncation passed");
    }
}
