package fun.acat.phonerelay;

import java.io.*;
public class FrameCreditTest {
    public static void main(String[] args) throws Exception {
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();DataOutputStream output=new DataOutputStream(bytes);
        output.writeInt(0x4641434b);output.writeLong(7);
        FrameCredit.await(new ByteArrayInputStream(bytes.toByteArray()),7);
        try {FrameCredit.await(new ByteArrayInputStream(bytes.toByteArray()),8);throw new AssertionError("wrong sequence accepted");}
        catch(IOException expected) { }
        byte[] bad=bytes.toByteArray();bad[0]=0;
        try {FrameCredit.await(new ByteArrayInputStream(bad),7);throw new AssertionError("wrong magic accepted");}
        catch(IOException expected) { }
        try {FrameCredit.await(new ByteArrayInputStream(new byte[3]),7);throw new AssertionError("truncation accepted");}
        catch(EOFException expected) { }
        System.out.println("FrameCredit: sequence, framing and truncation passed");
    }
}
