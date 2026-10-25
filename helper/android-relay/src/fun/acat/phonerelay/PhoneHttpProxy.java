package fun.acat.phonerelay;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.GZIPInputStream;
import javax.net.ssl.*;

/** 回环HTTP(S)代理：流式转发正文，采集有界文本，校验上游TLS身份。 */
final class PhoneHttpProxy implements AutoCloseable {
    interface Observer { void flow(String method, String url, int status, String type, String request, String body); }
    private final ProxyCertificates certificates;
    private final Observer observer;
    private final SSLSocketFactory upstreamTls;
    private final int bodyChars;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final ExecutorService workers = new ThreadPoolExecutor(0, 16, 30, TimeUnit.SECONDS, new SynchronousQueue<Runnable>());
    private final AtomicInteger failures = new AtomicInteger();
    private ServerSocket listener;
    private volatile String lastError = "";
    private volatile boolean running;

    PhoneHttpProxy(ProxyCertificates certificates, int bodyChars, Observer observer) {
        this(certificates, bodyChars, observer, (SSLSocketFactory) SSLSocketFactory.getDefault());
    }
    PhoneHttpProxy(ProxyCertificates certificates, int bodyChars, Observer observer, SSLSocketFactory upstreamTls) {
        this.certificates = certificates; this.observer = observer; this.upstreamTls = upstreamTls;
        this.bodyChars = Math.max(200, Math.min(8000, bodyChars));
    }
    void start(int port) throws IOException {
        listener = new ServerSocket(); listener.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 16); running = true;
        Thread accept = new Thread(() -> {
            while (running) try {
                Socket client = listener.accept(); client.setSoTimeout(30000); sockets.add(client);
                try { workers.execute(() -> serve(client)); } catch (RejectedExecutionException e) { sockets.remove(client); client.close(); failures.incrementAndGet(); }
            } catch (IOException e) { if (running) failures.incrementAndGet(); }
        }, "relay-capture"); accept.setDaemon(true); accept.start();
    }
    int failures() { return failures.get(); }
    String lastError() { return lastError; }
    boolean running() { return running; }
    private void serve(Socket original) {
        Socket client = original;
        try {
            Message request = Message.read(client.getInputStream());
            if (request == null) return;
            String[] start = request.start.split(" ", 3);
            if (start.length != 3 || !start[2].startsWith("HTTP/1.")) throw new IOException("请求行无效");
            boolean encrypted = start[0].equals("CONNECT");
            URI uri;
            if (encrypted) {
                uri = target("https://" + start[1]);
                client.getOutputStream().write("HTTP/1.1 200 Connection Established\r\n\r\n".getBytes(StandardCharsets.US_ASCII)); client.getOutputStream().flush();
                SSLSocket tls = (SSLSocket) certificates.site(uri.getHost()).getSocketFactory().createSocket(client, uri.getHost(), uri.getPort() < 0 ? 443 : uri.getPort(), true);
                client = tls; sockets.add(tls); tls.setUseClientMode(false); tls.setSoTimeout(30000); tls.startHandshake();
                request = Message.read(tls.getInputStream()); if (request == null) return;
                start = request.start.split(" ", 3);
                if (start.length != 3 || !start[1].startsWith("/")) throw new IOException("TLS请求行无效");
                uri = target("https://" + uri.getRawAuthority() + start[1]);
            } else uri = target(start[1]);
            int port = uri.getPort() >= 0 ? uri.getPort() : uri.getScheme().equals("https") ? 443 : 80;
            try (Socket upstream = connect(uri.getHost(), port, uri.getScheme().equals("https"))) {
                sockets.add(upstream);
                try {
                    OutputStream output = upstream.getOutputStream();
                    String path = uri.getRawPath(); if (path == null || path.isEmpty()) path = "/";
                    if (uri.getRawQuery() != null) path += "?" + uri.getRawQuery();
                    if (request.has("expect")) {
                        client.getOutputStream().write("HTTP/1.1 100 Continue\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
                        client.getOutputStream().flush(); request.headers.remove("expect");
                    }
                    request.headers.put("host", uri.getRawAuthority());
                    boolean upgrade = "websocket".equalsIgnoreCase(request.get("upgrade"));
                    request.write(output, start[0] + " " + path + " HTTP/1.1", upgrade);
                    byte[] req = request.body(client.getInputStream(), output, false, 800); output.flush();
                    Message response;
                    int status;
                    do {
                        response = Message.read(upstream.getInputStream()); if (response == null) throw new EOFException("上游未返回HTTP状态");
                        String[] parts = response.start.split(" ", 3); if (parts.length < 2) throw new IOException("响应行无效");
                        status = Integer.parseInt(parts[1]);
                        response.write(client.getOutputStream(), response.start, status == 101);
                        client.getOutputStream().flush();
                    } while (status < 200 && status != 101);
                    String type = response.get("content-type");
                    if (status == 101) {
                        observer.flow(start[0], uri.toASCIIString(), status, type, text(req, request, 800), "");
                        final Socket peer = client;
                        Thread upload = new Thread(() -> { try { copy(peer.getInputStream(), upstream.getOutputStream()); } catch (IOException e) { shutdown(upstream); } }, "relay-websocket");
                        upload.setDaemon(true); upload.start(); copy(upstream.getInputStream(), client.getOutputStream());
                    } else {
                        boolean noBody = start[0].equals("HEAD") || status == 204 || status == 304;
                        boolean captureText = textual(type);
                        if (!captureText) observer.flow(start[0], uri.toASCIIString(), status, type, text(req, request, 800), "");
                        byte[] body = noBody ? new byte[0] : response.body(upstream.getInputStream(), client.getOutputStream(), true, captureText ? 65536 : 0);
                        client.getOutputStream().flush();
                        if (captureText) observer.flow(start[0], uri.toASCIIString(), status, type, text(req, request, 800), text(body, response, bodyChars));
                    }
                } finally { sockets.remove(upstream); }
            }
        } catch (Exception e) { failures.incrementAndGet(); lastError = e.getClass().getSimpleName() + ": " + e.getMessage(); }
        finally { shutdown(client); shutdown(original); sockets.remove(client); sockets.remove(original); }
    }
    private Socket connect(String host, int port, boolean encrypted) throws IOException {
        Socket socket = new Socket();
        try {
            socket.connect(new InetSocketAddress(host, port), 10000); socket.setSoTimeout(30000); socket.setTcpNoDelay(true);
            if (!encrypted) return socket;
            SSLSocket tls = (SSLSocket) upstreamTls.createSocket(socket, host, port, true);
            SSLParameters params = tls.getSSLParameters(); params.setEndpointIdentificationAlgorithm("HTTPS");
            tls.setSSLParameters(params); tls.startHandshake(); return tls;
        } catch (IOException e) { socket.close(); throw e; }
    }
    private static URI target(String raw) throws IOException {
        try {
            URI uri = new URI(raw);
            if (!("http".equals(uri.getScheme()) || "https".equals(uri.getScheme())) || uri.getHost() == null || uri.getUserInfo() != null || uri.getFragment() != null
                    || uri.getPort() == 0 || uri.getPort() > 65535) throw new IOException("代理目标无效");
            return uri;
        } catch (URISyntaxException e) { throw new IOException("代理地址无效", e); }
    }
    private static boolean textual(String type) {
        String t = type.toLowerCase(Locale.ROOT);
        return t.contains("text") || t.contains("json") || t.contains("xml") || t.contains("javascript") || t.contains("x-www-form-urlencoded");
    }
    private static String text(byte[] bytes, Message message, int limit) {
        if (!textual(message.get("content-type"))) return "";
        String encoding = message.get("content-encoding");
        if (!encoding.isEmpty() && !encoding.equalsIgnoreCase("identity") && !encoding.equalsIgnoreCase("gzip")) return "";
        try {
            InputStream in = new ByteArrayInputStream(bytes);
            if (message.get("content-encoding").equalsIgnoreCase("gzip")) in = new GZIPInputStream(in);
            ByteArrayOutputStream out = new ByteArrayOutputStream(); byte[] buf = new byte[1024]; int n;
            while (out.size() < limit * 4 && (n = in.read(buf, 0, Math.min(buf.length, limit * 4 - out.size()))) > 0) out.write(buf, 0, n);
            String value = new String(out.toByteArray(), StandardCharsets.UTF_8); return value.substring(0, Math.min(limit, value.length()));
        } catch (IOException e) { return ""; }
    }
    private static void shutdown(Socket socket) { if (socket != null) try { socket.close(); } catch (IOException ignored) { } }
    private static void copy(InputStream in, OutputStream out) throws IOException { byte[] bytes = new byte[16384]; int n; while ((n = in.read(bytes)) >= 0) { out.write(bytes, 0, n); out.flush(); } }
    public void close() { running = false; if (listener != null) try { listener.close(); } catch (IOException ignored) { } for (Socket socket : sockets) shutdown(socket); sockets.clear(); workers.shutdownNow(); }

    static final class Message {
        final String start;
        final Map<String, String> headers = new LinkedHashMap<>();
        Message(String start) { this.start = start; }
        String get(String key) { String value = headers.get(key); return value == null ? "" : value; }
        boolean has(String key) { return headers.containsKey(key); }
        static String line(InputStream in) throws IOException {
            ByteArrayOutputStream out = new ByteArrayOutputStream(); int ch;
            while ((ch = in.read()) >= 0) {
                if (ch == 13) { if (in.read() != 10) throw new IOException("HTTP换行无效"); return out.toString("ISO-8859-1"); }
                if (ch == 0 || ch == 10 || out.size() >= 8192) throw new IOException("HTTP行无效"); out.write(ch);
            }
            if (out.size() != 0) throw new EOFException("HTTP行被截断"); return null;
        }
        static Message read(InputStream in) throws IOException {
            String first = line(in); if (first == null) return null; Message msg = new Message(first); int total = first.length();
            while (true) {
                String line = line(in); if (line == null) throw new EOFException("HTTP头被截断");
                total += line.length(); if (total > 65536 || msg.headers.size() >= 128) throw new IOException("HTTP头过大");
                if (line.isEmpty()) break;
                int colon = line.indexOf(':'); if (colon < 1) throw new IOException("HTTP头无效");
                String name = line.substring(0, colon).toLowerCase(Locale.ROOT);
                if (!name.matches("[!#$%&'*+.^_`|~0-9a-z-]+")) throw new IOException("HTTP头名称无效");
                String value = line.substring(colon + 1).trim();
                if (msg.has(name) && (name.equals("content-length") || name.equals("transfer-encoding"))) throw new IOException("HTTP长度头重复");
                msg.headers.put(name, msg.has(name) ? msg.get(name) + (name.equals("set-cookie") ? "\r\nset-cookie: " : ", ") + value : value);
            }
            if (msg.has("content-length") && msg.has("transfer-encoding")) throw new IOException("HTTP长度冲突");
            if (msg.has("content-length") && !msg.get("content-length").matches("[0-9]{1,18}")) throw new IOException("HTTP长度无效");
            if (msg.has("transfer-encoding") && !msg.get("transfer-encoding").equalsIgnoreCase("chunked")) throw new IOException("HTTP传输编码无效");
            return msg;
        }
        void write(OutputStream out, String first, boolean upgrade) throws IOException {
            StringBuilder header = new StringBuilder(first).append("\r\n");
            for (Map.Entry<String, String> entry : headers.entrySet()) {
                String name = entry.getKey();
                if (name.equals("proxy-authorization") || name.equals("proxy-connection") || name.equals("connection")) continue;
                header.append(name).append(": ").append(entry.getValue()).append("\r\n");
            }
            header.append(upgrade ? "connection: Upgrade\r\n\r\n" : "connection: close\r\n\r\n");
            out.write(header.toString().getBytes(StandardCharsets.ISO_8859_1));
        }
        byte[] body(InputStream in, OutputStream out, boolean untilEof, int limit) throws IOException {
            ByteArrayOutputStream preview = new ByteArrayOutputStream();
            if (has("transfer-encoding")) {
                while (true) {
                    String chunk = line(in); if (chunk == null || chunk.length() > 1024) throw new IOException("chunk头无效");
                    String size = chunk.split(";", 2)[0]; if (!size.matches("[0-9a-fA-F]{1,12}")) throw new IOException("chunk长度无效");
                    long length = Long.parseLong(size, 16); out.write((chunk + "\r\n").getBytes(StandardCharsets.US_ASCII));
                    if (length == 0) {
                        int count = 0;
                        while (true) { String trailer = line(in); if (trailer == null || (count += trailer.length() + 2) > 16384) throw new IOException("chunk尾部无效"); out.write((trailer + "\r\n").getBytes(StandardCharsets.ISO_8859_1)); if (trailer.isEmpty()) break; }
                        break;
                    }
                    transfer(in, out, length, preview, limit);
                    if (!"".equals(line(in))) throw new IOException("chunk终止无效"); out.write(new byte[]{13, 10});
                }
            } else if (has("content-length")) transfer(in, out, Long.parseLong(get("content-length")), preview, limit);
            else if (untilEof) transfer(in, out, -1, preview, limit);
            return preview.toByteArray();
        }
        private static void transfer(InputStream in, OutputStream out, long remaining, ByteArrayOutputStream preview, int limit) throws IOException {
            byte[] bytes = new byte[16384];
            while (remaining != 0) {
                int n = in.read(bytes, 0, remaining < 0 ? bytes.length : (int) Math.min(bytes.length, remaining));
                if (n < 0) { if (remaining > 0) throw new EOFException("HTTP正文被截断"); break; }
                out.write(bytes, 0, n); out.flush(); if (preview.size() < limit) preview.write(bytes, 0, Math.min(n, limit - preview.size()));
                if (remaining > 0) remaining -= n;
            }
        }
    }
}
