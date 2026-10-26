package fun.acat.phonerelay;
import java.util.Arrays;
public class H264ConfigTest {
    public static void main(String[] args) throws Exception {
        H264Config config = new H264Config();
        config.accept(new byte[]{0,0,0,1,0x67,20,30,0,0,1,0x68,40});
        if (!Arrays.equals(config.sps,new byte[]{0,0,0,1,0x67,20,30}) || !Arrays.equals(config.pps,new byte[]{0,0,1,0x68,40})) throw new AssertionError("SPS/PPS boundary");
        try { config.accept(new byte[]{0,0,1}); throw new AssertionError("truncated NAL accepted"); }
        catch(java.io.IOException expected) { }
        try { config.accept(new byte[]{4,5,6}); throw new AssertionError("invalid config accepted"); }
        catch(java.io.IOException expected) { }
        System.out.println("H264 SPS/PPS mixed Annex-B prefixes and truncation passed");
    }
}
