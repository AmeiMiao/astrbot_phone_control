package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testKey(seed byte) string {
	return base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{seed}, 32))
}

// validConfigJSON 返回一份合法的配置 JSON（覆盖常见字段）。
func validConfigJSON() map[string]any {
	return map[string]any{
		"interface": map[string]any{
			"private_key": testKey(0x11),
			"addresses":   []string{"10.66.0.2/24"},
			"listen_port": 0,
			"mtu":         1420,
		},
		"peers": []any{
			map[string]any{
				"public_key":                  testKey(0x22),
				"preshared_key":               "",
				"endpoint":                    "192.168.250.50:51820",
				"allowed_ips":                 []string{"10.66.0.0/24"},
				"persistent_keepalive_seconds": 25,
			},
		},
		"forwards": []any{
			map[string]any{"listen": "127.0.0.1:15555", "target": "10.66.0.3:5555"},
		},
		"log_level": "info",
	}
}

func writeConfigFile(t *testing.T, value any) string {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal config: %v", err)
	}
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return path
}

func TestLoadConfigValid(t *testing.T) {
	cfg, err := loadConfig(writeConfigFile(t, validConfigJSON()))
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if got := len(cfg.Peers); got != 1 {
		t.Fatalf("peers = %d, want 1", got)
	}
	if cfg.Peers[0].publicKey != [32]byte(bytes.Repeat([]byte{0x22}, 32)) {
		t.Errorf("peer public key 未正确解析")
	}
	if got := len(cfg.Forwards); got != 1 || cfg.Forwards[0].listenPort != 15555 {
		t.Errorf("forward 未正确解析: %+v", cfg.Forwards)
	}
	if cfg.Forwards[0].targetAddr.String() != "10.66.0.3:5555" {
		t.Errorf("forward target = %v", cfg.Forwards[0].targetAddr)
	}
	if cfg.Interface.MTU != 1420 {
		t.Errorf("mtu = %d, want 1420", cfg.Interface.MTU)
	}
}

func TestLoadConfigDefaults(t *testing.T) {
	raw := validConfigJSON()
	iface := raw["interface"].(map[string]any)
	delete(iface, "mtu")
	delete(iface, "listen_port")
	delete(raw, "log_level")
	cfg, err := loadConfig(writeConfigFile(t, raw))
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg.Interface.MTU != defaultMTU {
		t.Errorf("默认 mtu = %d, want %d", cfg.Interface.MTU, defaultMTU)
	}
	if cfg.Interface.ListenPort != 0 {
		t.Errorf("默认 listen_port = %d, want 0", cfg.Interface.ListenPort)
	}
}

func TestLoadConfigIgnoresUnknownFields(t *testing.T) {
	raw := validConfigJSON()
	raw["future_field"] = map[string]any{"anything": true}
	cfg, err := loadConfig(writeConfigFile(t, raw))
	if err != nil {
		t.Fatalf("未知字段应被忽略，实际报错: %v", err)
	}
	if cfg == nil {
		t.Fatal("config 为空")
	}
}

func TestLoadConfigInvalid(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(raw map[string]any)
		wantErr string
	}{
		{"invalid json", nil, ""},
		{"missing private key", func(raw map[string]any) {
			delete(raw["interface"].(map[string]any), "private_key")
		}, "private_key"},
		{"invalid base64 key", func(raw map[string]any) {
			raw["interface"].(map[string]any)["private_key"] = "not-base64!!!"
		}, "private_key"},
		{"short key", func(raw map[string]any) {
			raw["interface"].(map[string]any)["private_key"] = base64.StdEncoding.EncodeToString([]byte("short"))
		}, "32"},
		{"empty addresses", func(raw map[string]any) {
			raw["interface"].(map[string]any)["addresses"] = []string{}
		}, "addresses"},
		{"invalid address cidr", func(raw map[string]any) {
			raw["interface"].(map[string]any)["addresses"] = []string{"10.66.0.2"}
		}, "CIDR"},
		{"listen port out of range", func(raw map[string]any) {
			raw["interface"].(map[string]any)["listen_port"] = 70000
		}, "listen_port"},
		{"invalid mtu", func(raw map[string]any) {
			raw["interface"].(map[string]any)["mtu"] = -1
		}, "mtu"},
		{"empty allowed ips", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["allowed_ips"] = []string{}
		}, "allowed_ips"},
		{"invalid allowed ip", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["allowed_ips"] = []string{"10.66.0.0/33"}
		}, "CIDR"},
		{"invalid endpoint", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["endpoint"] = "192.168.250.50"
		}, "endpoint"},
		{"endpoint port zero", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["endpoint"] = "192.168.250.50:0"
		}, "端口"},
		{"invalid preshared key", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["preshared_key"] = "xxx"
		}, "preshared_key"},
		{"keepalive out of range", func(raw map[string]any) {
			raw["peers"].([]any)[0].(map[string]any)["persistent_keepalive_seconds"] = 70000
		}, "persistent_keepalive_seconds"},
		{"duplicate forward listen", func(raw map[string]any) {
			raw["forwards"] = []any{
				map[string]any{"listen": "127.0.0.1:15555", "target": "10.66.0.3:5555"},
				map[string]any{"listen": "127.0.0.1:15555", "target": "10.66.0.3:5556"},
			}
		}, "重复"},
		{"forward listen not loopback", func(raw map[string]any) {
			raw["forwards"].([]any)[0].(map[string]any)["listen"] = "0.0.0.0:15555"
		}, "127.0.0.1"},
		{"forward target outside allowed ips", func(raw map[string]any) {
			raw["forwards"].([]any)[0].(map[string]any)["target"] = "10.99.0.3:5555"
		}, "allowed_ips"},
		{"invalid forward target", func(raw map[string]any) {
			raw["forwards"].([]any)[0].(map[string]any)["target"] = "telephone:5555"
		}, "target"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.mutate == nil {
				path := filepath.Join(t.TempDir(), "config.json")
				if err := os.WriteFile(path, []byte("{invalid json"), 0o600); err != nil {
					t.Fatalf("write config: %v", err)
				}
				if _, err := loadConfig(path); err == nil {
					t.Fatal("非法 JSON 应返回错误")
				}
				return
			}
			raw := validConfigJSON()
			tc.mutate(raw)
			_, err := loadConfig(writeConfigFile(t, raw))
			if err == nil {
				t.Fatalf("期望配置错误包含 %q，实际未报错", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("错误信息 %q 未包含 %q", err.Error(), tc.wantErr)
			}
		})
	}
}

func TestLoadConfigMissingFile(t *testing.T) {
	if _, err := loadConfig(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("配置文件不存在时应返回错误")
	}
}

func TestBuildPeersIPC(t *testing.T) {
	cfg, err := loadConfig(writeConfigFile(t, validConfigJSON()))
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	ipc := buildPeersIPC(cfg)
	wantLines := []string{
		"replace_peers=true",
		"public_key=" + hexKey([32]byte(bytes.Repeat([]byte{0x22}, 32))),
		"endpoint=192.168.250.50:51820",
		"persistent_keepalive_interval=25",
		"replace_allowed_ips=true",
		"allowed_ip=10.66.0.0/24",
	}
	for _, line := range wantLines {
		if !strings.Contains(ipc, line+"\n") {
			t.Errorf("IPC 配置缺少行 %q，实际:\n%s", line, ipc)
		}
	}

	deviceIPC := buildDeviceIPC(cfg)
	if !strings.Contains(deviceIPC, "private_key="+hexKey([32]byte(bytes.Repeat([]byte{0x11}, 32)))) {
		t.Errorf("设备 IPC 私钥不正确: %s", deviceIPC)
	}
	if !strings.Contains(deviceIPC, "listen_port=0") {
		t.Errorf("设备 IPC 缺少 listen_port: %s", deviceIPC)
	}
}

func TestParseKeyBase64(t *testing.T) {
	key := bytes.Repeat([]byte{0xab}, 32)
	encoded := base64.StdEncoding.EncodeToString(key)

	parsed, err := parseKeyBase64(encoded)
	if err != nil {
		t.Fatalf("标准 base64 解析失败: %v", err)
	}
	if !bytes.Equal(parsed[:], key) {
		t.Fatalf("解析结果不一致")
	}

	withoutPadding := strings.TrimRight(encoded, "=")
	if _, err := parseKeyBase64(withoutPadding); err != nil {
		t.Fatalf("无填充 base64 应可解析: %v", err)
	}

	for _, invalid := range []string{"", "abc", "!!!", base64.StdEncoding.EncodeToString([]byte("too-short"))} {
		if _, err := parseKeyBase64(invalid); err == nil {
			t.Errorf("%q 应解析失败", invalid)
		}
	}
}

func TestPublicKeyBase64(t *testing.T) {
	privateKey, err := parseKeyBase64(testKey(0x11))
	if err != nil {
		t.Fatalf("parse private key: %v", err)
	}
	publicKey, err := publicKeyBase64(privateKey)
	if err != nil {
		t.Fatalf("publicKeyBase64: %v", err)
	}
	raw, err := base64.StdEncoding.DecodeString(publicKey)
	if err != nil || len(raw) != 32 {
		t.Fatalf("公钥输出非法: %q (len=%d, err=%v)", publicKey, len(raw), err)
	}

	// Curve25519 计算公钥时会 clamp 私钥，未 clamp 的私钥应得到相同公钥。
	unclamped := privateKey
	unclamped[0] = 0x02
	unclamped[31] = 0x44
	publicKeyUnclamped, err := publicKeyBase64(unclamped)
	if err != nil {
		t.Fatalf("publicKeyBase64(unclamped): %v", err)
	}
	clamped := unclamped
	clamped[0] &= 248
	clamped[31] &= 127
	clamped[31] |= 64
	publicKeyClamped, err := publicKeyBase64(clamped)
	if err != nil {
		t.Fatalf("publicKeyBase64(clamped): %v", err)
	}
	if publicKeyUnclamped != publicKeyClamped {
		t.Errorf("私钥 clamp 后公钥应相同: %q != %q", publicKeyUnclamped, publicKeyClamped)
	}
}

func TestParseIpcGet(t *testing.T) {
	text := `private_key=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
listen_port=51820
public_key=1111111111111111111111111111111111111111111111111111111111111111
preshared_key=0000000000000000000000000000000000000000000000000000000000000000
protocol_version=1
endpoint=192.168.250.50:51820
last_handshake_time_sec=1792510000
last_handshake_time_nsec=123
tx_bytes=5678
rx_bytes=1234
persistent_keepalive_interval=25
allowed_ip=10.66.0.0/24
`
	state := parseIpcGet(text)
	if state.listenPort != 51820 {
		t.Errorf("listen_port = %d, want 51820", state.listenPort)
	}
	peer, ok := state.peers["1111111111111111111111111111111111111111111111111111111111111111"]
	if !ok {
		t.Fatalf("未解析到 peer: %+v", state.peers)
	}
	if peer.endpoint != "192.168.250.50:51820" {
		t.Errorf("endpoint = %q", peer.endpoint)
	}
	if peer.lastHandshakeSec != 1792510000 {
		t.Errorf("last_handshake = %d", peer.lastHandshakeSec)
	}
	if peer.rxBytes != 1234 || peer.txBytes != 5678 {
		t.Errorf("rx/tx = %d/%d", peer.rxBytes, peer.txBytes)
	}
}

func TestStatusJSONFields(t *testing.T) {
	status := &statusFile{
		Running: true,
		Version: version,
		Interface: statusInterface{
			ListenPort: 51820,
			Addresses:  []string{"10.66.0.1/24"},
			PublicKey:  testKey(0x33),
		},
		Peers: []statusPeer{{
			PublicKey:         testKey(0x22),
			Endpoint:          "1.2.3.4:51820",
			AllowedIPs:        []string{"10.66.0.0/24"},
			LastHandshakeUnix: 1792510000,
			RxBytes:           1234,
			TxBytes:           5678,
		}},
		Forwards: []statusForward{{
			Listen:            "127.0.0.1:15555",
			Target:            "10.66.0.3:5555",
			ActiveConnections: 1,
			TotalConnections:  7,
			LastError:         "",
		}},
		UpdatedAtUnix: 1792510001,
	}

	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("marshal status: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal status: %v", err)
	}
	for _, key := range []string{"running", "version", "interface", "peers", "forwards", "updated_at_unix"} {
		if _, ok := decoded[key]; !ok {
			t.Errorf("状态 JSON 缺少顶层字段 %q", key)
		}
	}
	iface := decoded["interface"].(map[string]any)
	for _, key := range []string{"listen_port", "addresses", "public_key"} {
		if _, ok := iface[key]; !ok {
			t.Errorf("interface 缺少字段 %q", key)
		}
	}
	peer := decoded["peers"].([]any)[0].(map[string]any)
	for _, key := range []string{"public_key", "endpoint", "allowed_ips", "last_handshake_unix", "rx_bytes", "tx_bytes"} {
		if _, ok := peer[key]; !ok {
			t.Errorf("peer 缺少字段 %q", key)
		}
	}
	forward := decoded["forwards"].([]any)[0].(map[string]any)
	for _, key := range []string{"listen", "target", "active_connections", "total_connections", "last_error"} {
		if _, ok := forward[key]; !ok {
			t.Errorf("forward 缺少字段 %q", key)
		}
	}
}

func TestSnapshotEmptySlices(t *testing.T) {
	cfg, err := loadConfig(writeConfigFile(t, validConfigJSON()))
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	publicKey, err := publicKeyBase64(cfg.Interface.privateKey)
	if err != nil {
		t.Fatalf("publicKeyBase64: %v", err)
	}
	rt := &runtime{cfg: cfg, publicKey: publicKey}
	rt.cfg.Peers = nil
	rt.cfg.Forwards = nil
	status := rt.snapshotStatus(time.Unix(1792510001, 0))
	if status.Peers == nil || status.Forwards == nil {
		t.Fatal("peers/forwards 为空时应序列化为 [] 而不是 null")
	}
	if status.Running != true || status.Version != version {
		t.Errorf("running/version 不正确: %+v", status)
	}
	if status.Interface.PublicKey == "" {
		t.Errorf("interface.public_key 不应为空")
	}
}
