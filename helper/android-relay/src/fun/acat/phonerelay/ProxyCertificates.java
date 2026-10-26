package fun.acat.phonerelay;

import java.io.*;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.*;
import java.security.cert.*;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.*;
import javax.net.ssl.*;
import javax.security.auth.x500.X500Principal;
import org.bouncycastle.x509.X509V3CertificateGenerator;
import org.bouncycastle.asn1.x509.*;

/** 手机私有抓包CA与有界的站点证书缓存。 */
final class ProxyCertificates {
    private final KeyPair key;
    private final X509Certificate ca;
    private final Map<String, SSLContext> sites = new LinkedHashMap<String, SSLContext>(16, .75f, true) {
        protected boolean removeEldestEntry(Map.Entry<String, SSLContext> entry) { return size() > 64; }
    };
    static synchronized ProxyCertificates load(File directory) throws Exception { return new ProxyCertificates(directory); }
    byte[] encoded() throws Exception { return ca.getEncoded(); }
    ProxyCertificates(File directory) throws Exception {
        File identity = new File(directory, "capture-identity.bin");
        if (identity.isFile()) {
            try (DataInputStream in = new DataInputStream(new FileInputStream(identity))) {
                byte[] privateBytes = field(in, 8192), certBytes = field(in, 16384);
                if (in.read() != -1) throw new IOException("抓包CA身份文件包含额外数据");
                PrivateKey privateKey = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(privateBytes));
                ca = (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(certBytes));
                key = new KeyPair(ca.getPublicKey(), privateKey);
                Signature proof = Signature.getInstance("SHA256withRSA"); proof.initSign(privateKey); proof.update(new byte[]{1}); byte[] signed = proof.sign();
                proof.initVerify(ca.getPublicKey()); proof.update(new byte[]{1});
                if (!proof.verify(signed) || ca.getBasicConstraints() < 0) throw new IOException("抓包CA身份不匹配");
                ca.checkValidity();
            }
        } else {
            KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA"); generator.initialize(2048); key = generator.generateKeyPair();
            X509V3CertificateGenerator builder = base("CN=Phone Relay Capture CA", key.getPublic(), "CN=Phone Relay Capture CA", 730);
            builder.addExtension("2.5.29.19", true, new BasicConstraints(true));
            builder.addExtension("2.5.29.15", true, new KeyUsage(KeyUsage.keyCertSign | KeyUsage.cRLSign));
            ca = builder.generate(key.getPrivate());
            File temporary = File.createTempFile("capture-identity-", ".tmp", directory);
            temporary.setReadable(false, false); temporary.setReadable(true, true);
            temporary.setWritable(false, false); temporary.setWritable(true, true);
            try {
                try (FileOutputStream file = new FileOutputStream(temporary); DataOutputStream out = new DataOutputStream(file)) {
                    byte[] privateBytes = key.getPrivate().getEncoded(), certBytes = ca.getEncoded();
                    out.writeInt(privateBytes.length); out.write(privateBytes); out.writeInt(certBytes.length); out.write(certBytes);
                    out.flush(); file.getFD().sync();
                }
                Files.move(temporary.toPath(), identity.toPath(), java.nio.file.StandardCopyOption.ATOMIC_MOVE);
            } finally { if (temporary.exists() && !temporary.delete()) throw new IOException("清理CA临时文件失败"); }
        }
    }
    private static byte[] field(DataInputStream in, int maximum) throws IOException {
        int size = in.readInt(); if (size < 64 || size > maximum) throw new IOException("抓包CA身份长度无效");
        byte[] bytes = new byte[size]; in.readFully(bytes); return bytes;
    }
    private static X509V3CertificateGenerator base(String subject, PublicKey publicKey, String issuer, int days) {
        X509V3CertificateGenerator builder = new X509V3CertificateGenerator();
        builder.setSerialNumber(new BigInteger(128, new SecureRandom()).setBit(127));
        builder.setSubjectDN(new X500Principal(subject)); builder.setIssuerDN(new X500Principal(issuer));
        builder.setNotBefore(new Date(System.currentTimeMillis() - 86400000L));
        builder.setNotAfter(new Date(System.currentTimeMillis() + days * 86400000L));
        builder.setPublicKey(publicKey); builder.setSignatureAlgorithm("SHA256WithRSA"); return builder;
    }
    String pem() throws Exception {
        return "-----BEGIN CERTIFICATE-----\n" + Base64.getMimeEncoder(64, new byte[]{10}).encodeToString(ca.getEncoded()) + "\n-----END CERTIFICATE-----\n";
    }
    synchronized SSLContext site(String host) throws Exception {
        if (host.startsWith("[") && host.endsWith("]")) host = host.substring(1, host.length() - 1);
        SSLContext existing = sites.get(host); if (existing != null) return existing;
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA"); generator.initialize(2048); KeyPair leaf = generator.generateKeyPair();
        X509V3CertificateGenerator builder = base("CN=Phone Relay Captured Site", leaf.getPublic(), ca.getSubjectX500Principal().getName(), 30);
        builder.addExtension("2.5.29.19", true, new BasicConstraints(false));
        builder.addExtension("2.5.29.15", true, new KeyUsage(KeyUsage.digitalSignature | KeyUsage.keyEncipherment));
        builder.addExtension("2.5.29.37", false, new ExtendedKeyUsage(KeyPurposeId.id_kp_serverAuth));
        boolean ip = host.matches("[0-9.]+") || host.indexOf(':') >= 0;
        builder.addExtension("2.5.29.17", false, new GeneralNames(new GeneralName(ip ? GeneralName.iPAddress : GeneralName.dNSName, host)));
        X509Certificate certificate = builder.generate(key.getPrivate());
        KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType()); store.load(null, null);
        store.setKeyEntry("capture", leaf.getPrivate(), new char[0], new java.security.cert.Certificate[]{certificate, ca});
        KeyManagerFactory manager = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()); manager.init(store, new char[0]);
        SSLContext tls = SSLContext.getInstance("TLS"); tls.init(manager.getKeyManagers(), null, new SecureRandom());
        sites.put(host, tls); return tls;
    }
}
