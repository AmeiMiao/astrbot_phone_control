package fun.acat.phonerelay;

import java.io.IOException;

/** 有界在途字节，收到实际消费进度后释放发送额度。 */
final class StreamCredits implements AutoCloseable {
    private final int limit;
    private final long idleMillis;
    private long sent,received;
    private boolean closed;
    StreamCredits() {this(65536,15000);}
    StreamCredits(int limit,long idleMillis) {this.limit=limit;this.idleMillis=idleMillis;}
    synchronized void reserve(int count) throws IOException {
        if(count<=0 || count>limit)throw new IOException("数据块超过发送窗口");
        long progressAt=System.nanoTime(),known=received;
        while(!closed && sent-received+count>limit) {
            if(known!=received) {known=received;progressAt=System.nanoTime();}
            long remaining=idleMillis-(System.nanoTime()-progressAt)/1000000;
            if(remaining<=0)throw new IOException("数据接收确认无进度");
            try {wait(remaining);}catch(InterruptedException e) {Thread.currentThread().interrupt();throw new IOException("数据传输已取消",e);}
        }
        if(closed)throw new IOException("数据连接已结束");
        sent+=count;
    }
    synchronized void received(long count) throws IOException {
        if(closed)return;
        if(count<received || count>sent)throw new IOException("数据接收进度越界");
        received=count;notifyAll();
    }
    public synchronized void close() {closed=true;notifyAll();}
}
