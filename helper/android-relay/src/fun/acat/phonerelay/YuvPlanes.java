package fun.acat.phonerelay;

import java.io.IOException;
import java.nio.ByteBuffer;

/** 按裁剪区域、行距及像素步长批量复制YUV平面。 */
final class YuvPlanes {
    static byte[] nv21(ByteBuffer[] planes, int[] rows, int[] strides, int left, int top, int width, int height) throws IOException {
        if (width <= 0 || height <= 0 || left < 0 || top < 0 || ((width | height | left | top) & 1) != 0
            || (long)width * height > 9000000) throw new IOException("视频YUV尺寸无效");
        byte[] target = new byte[width * height * 3 / 2];
        try {
            for (int plane = 0; plane < 3; plane++) {
                int shift = plane == 0 ? 0 : 1, w = width >> shift, h = height >> shift;
                if (rows[plane] < 1 || strides[plane] < 1) throw new IOException("视频YUV步长无效");
                ByteBuffer source = planes[plane].duplicate();
                int origin = source.position() + (top >> shift) * rows[plane] + (left >> shift) * strides[plane];
                byte[] row = new byte[(w - 1) * strides[plane] + 1];
                for (int y = 0; y < h; y++) {
                    source.position(origin + y * rows[plane]); source.get(row);
                    if (plane == 0 && strides[plane] == 1) System.arraycopy(row, 0, target, y * width, w);
                    else for (int x = 0; x < w; x++) target[plane == 0 ? y * width + x
                        : width * height + y * width + x * 2 + (plane == 1 ? 1 : 0)] = row[x * strides[plane]];
                }
            }
        } catch (IllegalArgumentException | java.nio.BufferUnderflowException | IndexOutOfBoundsException e) {
            throw new IOException("视频YUV平面不完整", e);
        }
        return target;
    }
}
