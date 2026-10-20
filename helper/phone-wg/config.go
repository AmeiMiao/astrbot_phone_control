package main

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"strconv"
	"strings"
)

const (
	defaultMTU       = 1420
	minForwardPort   = 1
	maxForwardPort   = 65535
	maxListenPort    = 65535
	maxKeepaliveSecs = 65535
)

// config 对应插件写入的配置 JSON。未知字段由 encoding/json 默认忽略。
type config struct {
	Interface interfaceConfig `json:"interface"`
	Peers     []peerConfig    `json:"peers"`
	Forwards  []forwardConfig `json:"forwards"`
	LogLevel  string          `json:"log_level"`

	// prefixes 是 interface.addresses 解析后的前缀（供 netstack 使用）。
	prefixes []netip.Prefix
}

type interfaceConfig struct {
	PrivateKey string   `json:"private_key"`
	Addresses  []string `json:"addresses"`
	ListenPort int      `json:"listen_port"`
	MTU        int      `json:"mtu"`

	// 解析后的派生字段。
	privateKey [32]byte
}

type peerConfig struct {
	PublicKey                 string   `json:"public_key"`
	PresharedKey              string   `json:"preshared_key"`
	Endpoint                  string   `json:"endpoint"`
	AllowedIPs                []string `json:"allowed_ips"`
	PersistentKeepaliveSecond int      `json:"persistent_keepalive_seconds"`

	// 解析后的派生字段。
	publicKey       [32]byte
	presharedKey    [32]byte
	hasPresharedKey bool
	allowedPrefixes []netip.Prefix
}

type forwardConfig struct {
	Listen string `json:"listen"`
	Target string `json:"target"`

	// 解析后的派生字段。
	listenPort int
	targetAddr netip.AddrPort
}

// loadConfig 读取并校验配置文件；任何错误都视为配置错误（退出码 2）。
func loadConfig(path string) (*config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取配置文件失败: %w", err)
	}
	var cfg config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("解析配置 JSON 失败: %w", err)
	}
	if err := cfg.normalizeAndValidate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// normalizeAndValidate 校验配置并填充派生字段；非法值一律拒绝。
func (cfg *config) normalizeAndValidate() error {
	if err := cfg.validateInterface(); err != nil {
		return err
	}
	if err := cfg.validatePeers(); err != nil {
		return err
	}
	return cfg.validateForwards()
}

func (cfg *config) validateInterface() error {
	iface := &cfg.Interface

	if strings.TrimSpace(iface.PrivateKey) == "" {
		return errors.New("interface.private_key 不能为空")
	}
	privateKey, err := parseKeyBase64(iface.PrivateKey)
	if err != nil {
		return fmt.Errorf("interface.private_key 无效: %w", err)
	}
	iface.privateKey = privateKey

	if len(iface.Addresses) == 0 {
		return errors.New("interface.addresses 至少需要一个 CIDR 地址")
	}
	cfg.prefixes = make([]netip.Prefix, 0, len(iface.Addresses))
	for i, addr := range iface.Addresses {
		trimmed := strings.TrimSpace(addr)
		prefix, err := netip.ParsePrefix(trimmed)
		if err != nil {
			return fmt.Errorf("interface.addresses[%d] %q 不是合法 CIDR: %w", i, addr, err)
		}
		iface.Addresses[i] = trimmed
		cfg.prefixes = append(cfg.prefixes, prefix)
	}

	if iface.ListenPort < 0 || iface.ListenPort > maxListenPort {
		return fmt.Errorf("interface.listen_port %d 超出范围 0-%d", iface.ListenPort, maxListenPort)
	}
	if iface.MTU == 0 {
		iface.MTU = defaultMTU
	}
	if iface.MTU < 1 || iface.MTU > 65535 {
		return fmt.Errorf("interface.mtu %d 超出范围 1-65535", iface.MTU)
	}
	return nil
}

func (cfg *config) validatePeers() error {
	for i := range cfg.Peers {
		peer := &cfg.Peers[i]

		if strings.TrimSpace(peer.PublicKey) == "" {
			return fmt.Errorf("peers[%d].public_key 不能为空", i)
		}
		publicKey, err := parseKeyBase64(peer.PublicKey)
		if err != nil {
			return fmt.Errorf("peers[%d].public_key 无效: %w", i, err)
		}
		peer.publicKey = publicKey

		if raw := strings.TrimSpace(peer.PresharedKey); raw != "" {
			presharedKey, err := parseKeyBase64(raw)
			if err != nil {
				return fmt.Errorf("peers[%d].preshared_key 无效: %w", i, err)
			}
			peer.presharedKey = presharedKey
			peer.hasPresharedKey = true
		}

		if endpoint := strings.TrimSpace(peer.Endpoint); endpoint != "" {
			addrPort, err := netip.ParseAddrPort(endpoint)
			if err != nil {
				return fmt.Errorf("peers[%d].endpoint %q 不是合法 ip:port: %w", i, peer.Endpoint, err)
			}
			if addrPort.Port() == 0 {
				return fmt.Errorf("peers[%d].endpoint %q 端口不能为 0", i, peer.Endpoint)
			}
			peer.Endpoint = endpoint
		}

		if len(peer.AllowedIPs) == 0 {
			return fmt.Errorf("peers[%d].allowed_ips 不能为空", i)
		}
		peer.allowedPrefixes = make([]netip.Prefix, 0, len(peer.AllowedIPs))
		for j, cidr := range peer.AllowedIPs {
			trimmed := strings.TrimSpace(cidr)
			prefix, err := netip.ParsePrefix(trimmed)
			if err != nil {
				return fmt.Errorf("peers[%d].allowed_ips[%d] %q 不是合法 CIDR: %w", i, j, cidr, err)
			}
			peer.AllowedIPs[j] = trimmed
			peer.allowedPrefixes = append(peer.allowedPrefixes, prefix)
		}

		if peer.PersistentKeepaliveSecond < 0 || peer.PersistentKeepaliveSecond > maxKeepaliveSecs {
			return fmt.Errorf("peers[%d].persistent_keepalive_seconds %d 超出范围 0-%d",
				i, peer.PersistentKeepaliveSecond, maxKeepaliveSecs)
		}
	}
	return nil
}

func (cfg *config) validateForwards() error {
	usedPorts := make(map[int]int, len(cfg.Forwards))
	for i := range cfg.Forwards {
		forward := &cfg.Forwards[i]

		listen := strings.TrimSpace(forward.Listen)
		host, portStr, err := net.SplitHostPort(listen)
		if err != nil {
			return fmt.Errorf("forwards[%d].listen %q 不是合法 host:port: %w", i, forward.Listen, err)
		}
		if host != "127.0.0.1" {
			return fmt.Errorf("forwards[%d].listen %q 必须是 127.0.0.1:<port>", i, forward.Listen)
		}
		port, err := strconv.Atoi(portStr)
		if err != nil || port < minForwardPort || port > maxForwardPort {
			return fmt.Errorf("forwards[%d].listen %q 端口无效（要求 1-%d）", i, forward.Listen, maxForwardPort)
		}
		if previous, ok := usedPorts[port]; ok {
			return fmt.Errorf("forwards[%d].listen 与 forwards[%d] 重复监听端口 %d", i, previous, port)
		}
		usedPorts[port] = i
		forward.listenPort = port
		forward.Listen = listen

		target := strings.TrimSpace(forward.Target)
		addrPort, err := netip.ParseAddrPort(target)
		if err != nil {
			return fmt.Errorf("forwards[%d].target %q 不是合法 ip:port: %w", i, forward.Target, err)
		}
		if addrPort.Port() == 0 {
			return fmt.Errorf("forwards[%d].target %q 端口不能为 0", i, forward.Target)
		}
		if !cfg.targetCoveredByPeers(addrPort.Addr()) {
			return fmt.Errorf("forwards[%d].target %q 的地址不在任何 peer 的 allowed_ips 内", i, forward.Target)
		}
		forward.targetAddr = addrPort
		forward.Target = target
	}
	return nil
}

// targetCoveredByPeers 检查转发目标地址是否落在任一 peer 的 allowed_ips 内。
func (cfg *config) targetCoveredByPeers(addr netip.Addr) bool {
	for i := range cfg.Peers {
		for _, prefix := range cfg.Peers[i].allowedPrefixes {
			if prefix.Contains(addr) {
				return true
			}
		}
	}
	return false
}

// parseKeyBase64 解析 WireGuard 标准 base64 密钥（32 字节），
// 同时接受省略尾部填充的写法。
func parseKeyBase64(s string) ([32]byte, error) {
	var out [32]byte
	s = strings.TrimSpace(s)
	if s == "" {
		return out, errors.New("密钥为空")
	}
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		raw, err = base64.RawStdEncoding.DecodeString(strings.TrimRight(s, "="))
		if err != nil {
			return out, fmt.Errorf("不是合法 base64: %w", err)
		}
	}
	if len(raw) != 32 {
		return out, fmt.Errorf("解码后长度为 %d 字节，要求 32 字节", len(raw))
	}
	copy(out[:], raw)
	return out, nil
}

// hexKey 返回 IPC 文本协议所需的私钥/公钥十六进制表示。
func hexKey(key [32]byte) string {
	return hex.EncodeToString(key[:])
}
