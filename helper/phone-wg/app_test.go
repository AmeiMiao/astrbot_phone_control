package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func TestAppIngressPolicy(t *testing.T) {
	for _, v := range []struct {
		port int
		peer string
	}{{-1, "10.66.0.3"}, {80, "10.66.0.3"}, {65536, "10.66.0.3"}, {6190, "192.0.2.1"}, {6190, "::1"}} {
		raw := validConfigJSON()
		raw["app_port"] = v.port
		raw["app_peer"] = v.peer
		if _, err := loadConfig(writeConfigFile(t, raw)); err == nil {
			t.Fatalf("错误接受App配置 %v", v)
		}
	}
}

// 通过真实用户态TCP栈转发已验证证书的TLS流，覆盖来源拒绝和运行时关闭。
func TestAppIngressTLSAndShutdown(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "relay-app-tls") }))
	defer server.Close()
	target, err := netip.ParseAddrPort(strings.TrimPrefix(server.URL, "https://"))
	if err != nil {
		t.Fatal(err)
	}
	raw := validConfigJSON()
	raw["peers"].([]any)[0].(map[string]any)["endpoint"] = ""
	raw["app_port"] = int(target.Port())
	raw["app_peer"] = "10.66.0.2"
	raw["forwards"] = []any{}
	cfg, err := loadConfig(writeConfigFile(t, raw))
	if err != nil {
		t.Fatal(err)
	}
	rt, err := startRuntime(cfg, newLogger("error"))
	if err != nil {
		t.Fatal(err)
	}
	defer rt.shutdown()
	remote, err := rt.tnet.DialContext(rt.ctx, "tcp", netip.AddrPortFrom(cfg.prefixes[0].Addr(), target.Port()).String())
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(server.Certificate())
	client := tls.Client(remote, &tls.Config{RootCAs: pool, ServerName: "127.0.0.1", MinVersion: tls.VersionTLS12})
	defer client.Close()
	client.SetDeadline(time.Now().Add(3 * time.Second))
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	client.Write([]byte("GET / HTTP/1.1\r\nHost: relay\r\nConnection: close\r\n\r\n"))
	body, err := io.ReadAll(client)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "relay-app-tls") {
		t.Fatalf("TLS数据丢失 %q", body)
	}
	active, err := rt.tnet.DialContext(rt.ctx, "tcp", netip.AddrPortFrom(cfg.prefixes[0].Addr(), target.Port()).String())
	if err != nil {
		t.Fatal(err)
	}
	activeTLS := tls.Client(active, &tls.Config{RootCAs: pool, ServerName: "127.0.0.1", MinVersion: tls.VersionTLS12})
	defer activeTLS.Close()
	activeTLS.SetDeadline(time.Now().Add(3 * time.Second))
	if err := activeTLS.Handshake(); err != nil {
		t.Fatal(err)
	}
	rt.shutdown()
	if _, err := activeTLS.Read(make([]byte, 1)); err == nil {
		t.Fatal("停止后活动TLS连接仍可读")
	}
	if _, err := rt.tnet.DialContext(rt.ctx, "tcp", netip.AddrPortFrom(cfg.prefixes[0].Addr(), target.Port()).String()); err == nil {
		t.Fatal("关闭后仍接受连接")
	}
}

func TestAppIngressOverEncryptedWireGuard(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "encrypted-wg-app") }))
	defer server.Close()
	target, _ := netip.ParseAddrPort(strings.TrimPrefix(server.URL, "https://"))
	serverPrivate, _ := parseKeyBase64(testKey(0x11))
	phonePrivate, _ := parseKeyBase64(testKey(0x33))
	serverPublic, _ := publicKeyBase64(serverPrivate)
	phonePublic, _ := publicKeyBase64(phonePrivate)
	raw := validConfigJSON()
	raw["forwards"] = []any{}
	raw["app_port"] = int(target.Port())
	raw["app_peer"] = "10.66.0.3"
	raw["peers"] = []any{map[string]any{"public_key": phonePublic, "allowed_ips": []string{"10.66.0.3/32"}}}
	cfg, err := loadConfig(writeConfigFile(t, raw))
	if err != nil {
		t.Fatal(err)
	}
	host, err := startRuntime(cfg, newLogger("error"))
	if err != nil {
		t.Fatal(err)
	}
	defer host.shutdown()
	phoneRaw := validConfigJSON()
	phoneRaw["forwards"] = []any{}
	phoneRaw["interface"] = map[string]any{"private_key": testKey(0x33), "addresses": []string{"10.66.0.3/24"}}
	phoneRaw["peers"] = []any{map[string]any{"public_key": serverPublic, "allowed_ips": []string{"10.66.0.2/32"}, "endpoint": netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(host.deviceState().listenPort)).String()}}
	phoneCfg, err := loadConfig(writeConfigFile(t, phoneRaw))
	if err != nil {
		t.Fatal(err)
	}
	phone, err := startRuntime(phoneCfg, newLogger("error"))
	if err != nil {
		t.Fatal(err)
	}
	defer phone.shutdown()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	remote, err := phone.tnet.DialContext(ctx, "tcp", netip.AddrPortFrom(cfg.prefixes[0].Addr(), target.Port()).String())
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(server.Certificate())
	client := tls.Client(remote, &tls.Config{RootCAs: pool, ServerName: "127.0.0.1", MinVersion: tls.VersionTLS12})
	defer client.Close()
	client.SetDeadline(time.Now().Add(3 * time.Second))
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	client.Write([]byte("GET / HTTP/1.1\r\nHost: relay\r\nConnection: close\r\n\r\n"))
	body, err := io.ReadAll(client)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "encrypted-wg-app") {
		t.Fatal("WireGuard入站TLS负载缺失")
	}
	if host.deviceState().peers == nil {
		t.Fatal("未建立WireGuard对端")
	}
}

func TestAppIngressRejectsOtherSource(t *testing.T) {
	raw := validConfigJSON()
	raw["peers"].([]any)[0].(map[string]any)["endpoint"] = ""
	raw["app_port"] = 6190
	raw["app_peer"] = "10.66.0.3"
	raw["forwards"] = []any{}
	cfg, err := loadConfig(writeConfigFile(t, raw))
	if err != nil {
		t.Fatal(err)
	}
	rt, err := startRuntime(cfg, newLogger("error"))
	if err != nil {
		t.Fatal(err)
	}
	defer rt.shutdown()
	c, err := rt.tnet.DialContext(rt.ctx, "tcp", "10.66.0.2:6190")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := c.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("未明确拒绝错误来源: %v", err)
	}
}
