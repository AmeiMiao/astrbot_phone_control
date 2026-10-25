package fun.acat.phonerelay;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ssl.*;

public final class PhoneHttpProxyTest {
    static void check(boolean value, String reason) { if (!value) throw new AssertionError(reason); }
    static void write(OutputStream out, String value) throws IOException { out.write(value.getBytes(StandardCharsets.UTF_8)); out.flush(); }
    static SSLContext trusted(ProxyCertificates certificates) throws Exception {
        KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType()); store.load(null, null);
        store.setCertificateEntry("test", CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(certificates.pem().getBytes(StandardCharsets.US_ASCII))));
        TrustManagerFactory trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()); trust.init(store);
        SSLContext tls = SSLContext.getInstance("TLS"); tls.init(null, trust.getTrustManagers(), null); return tls;
    }
    static Future<?> origin(ServerSocket server, ExecutorService worker) {
        return worker.submit(() -> {
            try (Socket client = server.accept()) {
                client.setSoTimeout(5000);
                PhoneHttpProxy.Message req = PhoneHttpProxy.Message.read(client.getInputStream());
                req.body(client.getInputStream(), new ByteArrayOutputStream(), false, 0);
                write(client.getOutputStream(), "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n7\r\n{\"v\":1}\r\n0\r\n\r\n");
            } catch (IOException e) { throw new UncheckedIOException(e); }
        });
    }
    public static void main(String[] args) throws Exception {
        File directory = Files.createTempDirectory(new File(args[0]).toPath(), "capture-test-").toFile();
        ProxyCertificates cert = new ProxyCertificates(directory);
        check(cert.pem().equals(new ProxyCertificates(directory).pem()), "persistent CA identity");
        SSLContext.setDefault(trusted(cert));
        List<String> flows = Collections.synchronizedList(new ArrayList<>());
        PhoneHttpProxy proxy = new PhoneHttpProxy(cert, 1200, (method,url,status,type,request,body) -> flows.add(method + " " + status + " " + body));
        ExecutorService worker = Executors.newSingleThreadExecutor();
        int port;
        try (ServerSocket reserve = new ServerSocket(0)) { port = reserve.getLocalPort(); }
        try {
            proxy.start(port);
            try (ServerSocket server = new ServerSocket(0); Socket client = new Socket("127.0.0.1", port)) {
                client.setSoTimeout(5000); Future<?> response = origin(server, worker);
                write(client.getOutputStream(), "GET http://127.0.0.1:" + server.getLocalPort() + "/capture HTTP/1.1\r\nHost: ignored\r\n\r\n");
                PhoneHttpProxy.Message message = PhoneHttpProxy.Message.read(client.getInputStream());
                ByteArrayOutputStream body = new ByteArrayOutputStream(); message.body(client.getInputStream(), body, true, 100);
                check(body.toString("UTF-8").contains("{\"v\":1}"), "HTTP chunked response"); response.get(6, TimeUnit.SECONDS);
            }
            try (SSLServerSocket server = (SSLServerSocket) cert.site("127.0.0.1").getServerSocketFactory().createServerSocket(0);
                 Socket connection = new Socket("127.0.0.1", port)) {
                connection.setSoTimeout(5000); Future<?> response = origin(server, worker);
                write(connection.getOutputStream(), "CONNECT 127.0.0.1:" + server.getLocalPort() + " HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
                PhoneHttpProxy.Message ready = PhoneHttpProxy.Message.read(connection.getInputStream()); check(ready.start.contains("200"), "CONNECT");
                try (SSLSocket client = (SSLSocket) trusted(cert).getSocketFactory().createSocket(connection, "127.0.0.1", server.getLocalPort(), true)) {
                    client.startHandshake();
                    write(client.getOutputStream(), "POST /secure HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\nContent-Length: 3\r\n\r\none");
                    PhoneHttpProxy.Message message = PhoneHttpProxy.Message.read(client.getInputStream());
                    ByteArrayOutputStream body = new ByteArrayOutputStream(); message.body(client.getInputStream(), body, true, 100);
                    check(body.toString("UTF-8").contains("{\"v\":1}"), "HTTPS response");
                }
                response.get(6, TimeUnit.SECONDS);
            }
            long deadline = System.currentTimeMillis() + 2000;
            while (flows.size() < 2 && System.currentTimeMillis() < deadline) Thread.sleep(10);
            check(flows.contains("GET 200 {\"v\":1}") && flows.contains("POST 200 {\"v\":1}"), "captured request and decoded response");
            try (SSLServerSocket server = (SSLServerSocket) cert.site("wrong.invalid").getServerSocketFactory().createServerSocket(0);
                 Socket connection = new Socket("127.0.0.1", port)) {
                connection.setSoTimeout(5000); Future<?> response = origin(server, worker);
                write(connection.getOutputStream(), "CONNECT 127.0.0.1:" + server.getLocalPort() + " HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
                PhoneHttpProxy.Message.read(connection.getInputStream());
                try (SSLSocket client = (SSLSocket) trusted(cert).getSocketFactory().createSocket(connection, "127.0.0.1", server.getLocalPort(), true)) {
                    client.startHandshake();
                    write(client.getOutputStream(), "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
                    try { check(PhoneHttpProxy.Message.read(client.getInputStream()) == null, "wrong upstream hostname accepted"); }
                    catch (IOException expected) { }
                }
                try { response.get(6, TimeUnit.SECONDS); throw new AssertionError("upstream TLS identity must be checked"); }
                catch (ExecutionException expected) { }
            }
            check(flows.size() == 2 && proxy.failures() == 1, "failed upstream must not become successful flow");
            for (String bad : new String[]{"GET / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n", "GET / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", "GET / HTTP/1.1\n\n"}) {
                try { PhoneHttpProxy.Message.read(new ByteArrayInputStream(bad.getBytes(StandardCharsets.US_ASCII))); throw new AssertionError("HTTP framing must reject ambiguity"); }
                catch (IOException expected) { }
            }

        } finally { proxy.close(); worker.shutdownNow(); for (File file : directory.listFiles()) file.delete(); directory.delete(); }
        System.out.println("PhoneHttpProxy: real HTTP/chunked, trusted HTTPS MITM, body capture and framing checks passed");
    }
}
