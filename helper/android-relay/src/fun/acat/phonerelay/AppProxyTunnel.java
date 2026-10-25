package fun.acat.phonerelay;

import java.io.*;
import java.net.*;
import java.util.Set;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

/** 手机回环代理通过独立的固定证书TLS连接进入插件mitmproxy。 */
final class AppProxyTunnel implements AutoCloseable {
    interface Dial { Socket open() throws Exception; }
    private final Dial dial;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final ExecutorService workers = new ThreadPoolExecutor(0, 16, 30, TimeUnit.SECONDS, new SynchronousQueue<Runnable>());
    private ServerSocket listener;
    private volatile boolean running;
    private final AtomicInteger failures = new AtomicInteger();
    private volatile String error = "";
    AppProxyTunnel(Dial dial) { this.dial = dial; }
    void start(int port) throws IOException {
        listener = new ServerSocket(); listener.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 16); running = true;
        Thread accept = new Thread(() -> {
            while (running) try {
                Socket client = listener.accept(); client.setSoTimeout(30000); sockets.add(client);
                try { workers.execute(() -> serve(client)); }
                catch (RejectedExecutionException e) { sockets.remove(client); client.close(); failures.incrementAndGet(); }
            } catch (IOException e) { if (running) { failures.incrementAndGet(); error = e.getClass().getSimpleName(); } }
        }, "relay-mitm-tunnel"); accept.setDaemon(true); accept.start();
    }
    private void serve(Socket client) {
        Socket upstream = null;
        try {
            upstream = dial.open(); upstream.setSoTimeout(30000); sockets.add(upstream);
            final Socket peer = upstream;
            Thread upload = new Thread(() -> {
                try { copy(client.getInputStream(), peer.getOutputStream()); }
                catch (IOException e) { closeSocket(peer); }
            }, "relay-proxy-upload"); upload.setDaemon(true); upload.start();
            copy(upstream.getInputStream(), client.getOutputStream());
        } catch (Exception e) { if (running) { failures.incrementAndGet(); error = e.getClass().getSimpleName() + ": " + e.getMessage(); } }
        finally { closeSocket(upstream); closeSocket(client); sockets.remove(client); if (upstream != null) sockets.remove(upstream); }
    }
    private static void copy(InputStream in, OutputStream out) throws IOException { byte[] bytes = new byte[16384]; int n; while ((n = in.read(bytes)) >= 0) { out.write(bytes, 0, n); out.flush(); } }
    private static void closeSocket(Socket socket) { if (socket != null) try { socket.close(); } catch (IOException ignored) { } }
    boolean running() { return running; }
    int failures() { return failures.get(); }
    String lastError() { return error; }
    public void close() { running = false; if (listener != null) try { listener.close(); } catch (IOException ignored) { } for (Socket socket : sockets) closeSocket(socket); sockets.clear(); workers.shutdownNow(); }
}
