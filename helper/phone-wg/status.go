package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"

	"golang.org/x/crypto/curve25519"
)

// statusFile 是周期写入的状态文件结构，字段与插件侧读取逻辑严格对齐。
type statusFile struct {
	Running       bool            `json:"running"`
	Version       string          `json:"version"`
	Interface     statusInterface `json:"interface"`
	Peers         []statusPeer    `json:"peers"`
	Forwards      []statusForward `json:"forwards"`
	UpdatedAtUnix int64           `json:"updated_at_unix"`
	AppPort       int             `json:"app_port"`
}

type statusInterface struct {
	ListenPort int      `json:"listen_port"`
	Addresses  []string `json:"addresses"`
	PublicKey  string   `json:"public_key"`
}

type statusPeer struct {
	PublicKey         string   `json:"public_key"`
	Endpoint          string   `json:"endpoint"`
	AllowedIPs        []string `json:"allowed_ips"`
	LastHandshakeUnix int64    `json:"last_handshake_unix"`
	RxBytes           int64    `json:"rx_bytes"`
	TxBytes           int64    `json:"tx_bytes"`
}

type statusForward struct {
	Listen            string `json:"listen"`
	Target            string `json:"target"`
	ActiveConnections int64  `json:"active_connections"`
	TotalConnections  int64  `json:"total_connections"`
	LastError         string `json:"last_error"`
}

// ipcPeerState 是 IPC get 输出中单个 peer 的运行时状态。
type ipcPeerState struct {
	endpoint         string
	lastHandshakeSec int64
	rxBytes          int64
	txBytes          int64
}

// ipcDeviceState 是 IPC get（WireGuard 配置文本协议）输出的解析结果。
type ipcDeviceState struct {
	listenPort int
	peers      map[string]ipcPeerState // key: 公钥的小写 hex
}

// parseIpcGet 解析 device.IpcGet() 的文本输出。
//
// 输出为 key=value 逐行文本：设备段（private_key/listen_port/fwmark）在前，
// 每个 peer 以 public_key= 行开始，随后是该 peer 的 endpoint/握手/流量/AllowedIP 行。
func parseIpcGet(text string) *ipcDeviceState {
	state := &ipcDeviceState{peers: make(map[string]ipcPeerState)}
	deviceSection := true
	currentPeer := ""
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSuffix(line, "\r")
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if key == "public_key" {
			deviceSection = false
			currentPeer = value
			state.peers[currentPeer] = ipcPeerState{}
			continue
		}
		if deviceSection {
			if key == "listen_port" {
				if port, err := strconv.Atoi(value); err == nil {
					state.listenPort = port
				}
			}
			continue
		}
		peer := state.peers[currentPeer]
		switch key {
		case "endpoint":
			peer.endpoint = value
		case "last_handshake_time_sec":
			if secs, err := strconv.ParseInt(value, 10, 64); err == nil {
				peer.lastHandshakeSec = secs
			}
		case "rx_bytes":
			if bytes, err := strconv.ParseInt(value, 10, 64); err == nil {
				peer.rxBytes = bytes
			}
		case "tx_bytes":
			if bytes, err := strconv.ParseInt(value, 10, 64); err == nil {
				peer.txBytes = bytes
			}
		}
		state.peers[currentPeer] = peer
	}
	return state
}

// publicKeyBase64 返回私钥对应的 WireGuard 公钥（base64）。
func publicKeyBase64(privateKey [32]byte) (string, error) {
	publicKey, err := curve25519.X25519(privateKey[:], curve25519.Basepoint)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(publicKey), nil
}

// writeStatusFile 以原子方式写入状态文件：先写 <path>.tmp，再 rename 覆盖目标文件。
func writeStatusFile(path string, status *statusFile) error {
	data, err := json.Marshal(status)
	if err != nil {
		return fmt.Errorf("序列化状态 JSON 失败: %w", err)
	}
	data = append(data, '\n')

	tmpPath := path + ".tmp"
	file, err := os.OpenFile(tmpPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("创建临时状态文件失败: %w", err)
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		return fmt.Errorf("写入临时状态文件失败: %w", err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("同步临时状态文件失败: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("关闭临时状态文件失败: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("替换状态文件失败: %w", err)
	}
	return nil
}
