package fun.acat.phonerelay;

import java.io.IOException;
import java.util.concurrent.*;
public class StreamCreditsTest {
    public static void main(String[] args) throws Exception {
        StreamCredits credits=new StreamCredits(8,1000);credits.reserve(8);
        ExecutorService worker=Executors.newSingleThreadExecutor();
        try {
            Future<?> next=worker.submit(()-> {try {credits.reserve(8);}catch(IOException e) {throw new RuntimeException(e);}});
            Thread.sleep(30);if(next.isDone())throw new AssertionError("unbounded send");
            credits.received(4);Thread.sleep(30);if(next.isDone())throw new AssertionError("partial acknowledgement overspent");
            credits.received(8);next.get(1,TimeUnit.SECONDS);
            try {credits.received(17);throw new AssertionError("excess credit");}catch(IOException expected) { }
            try {credits.received(7);throw new AssertionError("replayed credit");}catch(IOException expected) { }
            Future<?> waiting=worker.submit(()-> {try {credits.reserve(8);throw new AssertionError("closed window accepted");}catch(IOException expected) { }});
            Thread.sleep(30);credits.close();waiting.get(1,TimeUnit.SECONDS);
            StreamCredits idle=new StreamCredits(8,30);idle.reserve(8);
            try {idle.reserve(1);throw new AssertionError("stalled consumer accepted");}catch(IOException expected) { }
        } finally {worker.shutdownNow();credits.close();}
        System.out.println("StreamCredits: bounded window, partial progress, replay, close and idle passed");
    }
}
