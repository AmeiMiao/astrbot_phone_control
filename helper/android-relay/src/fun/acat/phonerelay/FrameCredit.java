package fun.acat.phonerelay;

import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;

/** 每次接收确认释放一张新帧，序号绑定当前认证数据连接。 */
final class FrameCredit {
    static void await(InputStream input,long sequence) throws IOException {
        DataInputStream data=new DataInputStream(input);
        if(data.readInt()!=0x4641434b || data.readLong()!=sequence)throw new IOException("视频帧接收确认无效");
    }
}
