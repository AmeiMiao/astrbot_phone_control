package fun.acat.phonerelay;

import java.io.IOException;
import java.util.Arrays;

/** 提取Annex-B配置中的SPS与PPS，供手机MP4封装使用。 */
final class H264Config {
    byte[] sps, pps;
    void accept(byte[] bytes) throws IOException {
        int at = start(bytes, 0);
        if (at < 0) throw new IOException("H264配置缺少起始码");
        while (at >= 0) {
            int prefix = bytes[at + 2] == 1 ? 3 : 4;
            if (at + prefix >= bytes.length) throw new IOException("H264配置不完整");
            int next = start(bytes, at + prefix);
            int type = bytes[at + prefix] & 31;
            byte[] nal = Arrays.copyOfRange(bytes, at, next < 0 ? bytes.length : next);
            if (type == 7) sps = nal; else if (type == 8) pps = nal;
            at = next;
        }
    }
    private static int start(byte[] bytes, int from) {
        for (int i = from; i + 2 < bytes.length; i++)
            if (bytes[i] == 0 && bytes[i+1] == 0 && (bytes[i+2] == 1 || i+3 < bytes.length && bytes[i+2] == 0 && bytes[i+3] == 1)) return i;
        return -1;
    }
}
