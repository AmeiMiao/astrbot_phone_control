package fun.acat.phonerelay;

import java.nio.ByteBuffer;
import java.util.Arrays;

public class YuvPlanesTest {
    public static void main(String[] args) throws Exception {
        ByteBuffer y = ByteBuffer.wrap(new byte[]{99, 0,1,2,3,9,9, 4,5,6,7,9,9, 8,9,10,11,9,9, 12,13,14,15,9,9}); y.position(1);
        ByteBuffer u = ByteBuffer.wrap(new byte[]{20,99,21,99,99,99,22,99,23});
        ByteBuffer v = ByteBuffer.wrap(new byte[]{30,99,31,99,99,99,32,99,33});
        byte[] result = YuvPlanes.nv21(new ByteBuffer[]{y,u,v}, new int[]{6,6,6},new int[]{1,2,2},2,2,2,2);
        if (!Arrays.equals(result,new byte[]{10,11,14,15,33,23})) throw new AssertionError(Arrays.toString(result));
        if (y.position()!=1) throw new AssertionError("source position changed");
        try {YuvPlanes.nv21(new ByteBuffer[]{y,u,v},new int[]{6,6,6},new int[]{1,2,2},2,2,4,4);throw new AssertionError("truncated frame accepted");}
        catch(java.io.IOException expected) { }
        byte[] nv21=new byte[]{0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,30,20,31,21,32,22,33,23};
        byte[] scaled=YuvPlanes.resize(nv21,4,4,2,2);
        if(!Arrays.equals(scaled,new byte[]{0,2,8,10,30,20}))throw new AssertionError(Arrays.toString(scaled));
        if(YuvPlanes.resize(nv21,4,4,4,4)!=nv21)throw new AssertionError("full size copied");
        try {YuvPlanes.resize(nv21,4,4,3,2);throw new AssertionError("odd dimensions accepted");}
        catch(java.io.IOException expected) { }
        System.out.println("YUV crop, row stride, pixel stride and truncation passed");
    }
}
