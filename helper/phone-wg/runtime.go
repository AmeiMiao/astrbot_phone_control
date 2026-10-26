package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/netstack"
)

const (
	// forwardDialTimeout 是单次 TCP 转发拨号经隧道连接目标地址的超时。
	forwardDialTimeout = 10 * time.Second
	// statusWriteInterval 是状态文件写入周期（契约要求 ≤ 2 秒）。
	statusWriteInterval = time.Second
	// shutdownTimeout 是优雅关闭时等待连接处理协程退出的最长时间。
	shutdownTimeout = 5 * time.Second
)

// runtime 持有 WireGuard 设备、netstack 与所有 TCP 转发。
type runtime struct {
	cfg       *config
	log       *logger
	dev       *device.Device
	tnet      *netstack.Net
	publicKey string
	forwards  []*forward
	socks     net.Listener
	app       net.Listener
	appSlots  chan struct{}

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	// conns 跟踪所有活动连接（本地侧与隧道侧），关闭时统一断开。
	conns sync.Map

	shutdownOnce sync.Once
}

// forward 是一条本地 TCP 监听到隧道目标地址的转发。
type forward struct {
	config   forwardConfig
	listener net.Listener
	active   atomic.Int64
	total    atomic.Int64
	errMu    sync.Mutex
	lastErr  string
}

// startRuntime 创建 userspace WireGuard 设备、配置 peer 并启动 TCP 转发。
// 返回的错误表示运行环境初始化失败（对应退出码 3）。
func startRuntime(cfg *config, log *logger) (*runtime, error) {
	ctx, cancel := context.WithCancel(context.Background())
	rt := &runtime{cfg: cfg, log: log, ctx: ctx, cancel: cancel}

	publicKey, err := publicKeyBase64(cfg.Interface.privateKey)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("推导 interface 公钥失败: %w", err)
	}
	rt.publicKey = publicKey

	addrs := make([]netip.Addr, 0, len(cfg.prefixes))
	for _, prefix := range cfg.prefixes {
		addrs = append(addrs, prefix.Addr())
	}
	tunDevice, tnet, err := netstack.CreateNetTUN(addrs, nil, cfg.Interface.MTU)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("创建 userspace tun/netstack 失败: %w", err)
	}
	rt.tnet = tnet
	rt.dev = device.NewDevice(tunDevice, conn.NewDefaultBind(), log.deviceLogger())

	// 先配置设备级参数（此时设备尚未 Up，listen_port 只记录不绑定），
	// 再 Up 绑定 UDP 端口，最后写入 peer——这样 persistent_keepalive
	// 会在设备就绪时立即触发一次握手，而不是等到首次业务流量。
	if err := rt.dev.IpcSet(buildDeviceIPC(cfg)); err != nil {
		rt.dev.Close()
		cancel()
		return nil, fmt.Errorf("配置 WireGuard 设备失败: %w", err)
	}
	if err := rt.dev.Up(); err != nil {
		rt.dev.Close()
		cancel()
		return nil, fmt.Errorf("启动 WireGuard 设备失败: %w", err)
	}
	if len(cfg.Peers) > 0 {
		if err := rt.dev.IpcSet(buildPeersIPC(cfg)); err != nil {
			rt.dev.Close()
			cancel()
			return nil, fmt.Errorf("配置 WireGuard peer 失败: %w", err)
		}
	}

	for i := range cfg.Forwards {
		fwd := &forward{config: cfg.Forwards[i]}
		if err := fwd.start(rt); err != nil {
			for _, started := range rt.forwards {
				started.listener.Close()
			}
			rt.dev.Close()
			cancel()
			return nil, fmt.Errorf("监听转发端口 %s 失败: %w", fwd.config.Listen, err)
		}
		rt.forwards = append(rt.forwards, fwd)
		log.infof("TCP 转发已就绪: %s -> %s", fwd.config.Listen, fwd.config.Target)
	}

	if cfg.SocksListen != "" {
		listener, err := net.Listen("tcp", cfg.SocksListen)
		if err != nil {
			rt.shutdown()
			return nil, fmt.Errorf("SOCKS 监听失败: %w", err)
		}
		rt.socks = listener
		rt.wg.Add(1)
		go rt.serveSocks()
	}
	if cfg.AppPort != 0 {
		listener, err := tnet.ListenTCPAddrPort(netip.AddrPortFrom(cfg.prefixes[0].Addr(), uint16(cfg.AppPort)))
		if err != nil {
			rt.shutdown()
			return nil, fmt.Errorf("App组网入口监听失败: %w", err)
		}
		rt.app = listener
		rt.appSlots = make(chan struct{}, 32)
		rt.wg.Add(1)
		go rt.serveApp()
	}
	if state := rt.deviceState(); state != nil && state.listenPort != 0 {
		log.infof("WireGuard 设备已就绪: listen_port=%d public_key=%s", state.listenPort, rt.publicKey)
	} else {
		log.infof("WireGuard 设备已就绪: public_key=%s", rt.publicKey)
	}
	return rt, nil
}

// buildDeviceIPC 生成设备级 IPC 配置（私钥与监听端口）。
func buildDeviceIPC(cfg *config) string {
	return fmt.Sprintf("private_key=%s\nlisten_port=%d\n",
		hexKey(cfg.Interface.privateKey), cfg.Interface.ListenPort)
}

// buildPeersIPC 生成 peer 级 IPC 配置（WireGuard 配置文本协议，密钥为 hex）。
func buildPeersIPC(cfg *config) string {
	var builder []byte
	builder = append(builder, "replace_peers=true\n"...)
	for i := range cfg.Peers {
		peer := &cfg.Peers[i]
		builder = append(builder, "public_key="+hexKey(peer.publicKey)+"\n"...)
		if peer.hasPresharedKey {
			builder = append(builder, "preshared_key="+hexKey(peer.presharedKey)+"\n"...)
		}
		if peer.Endpoint != "" {
			builder = append(builder, "endpoint="+peer.Endpoint+"\n"...)
		}
		if peer.PersistentKeepaliveSecond > 0 {
			builder = append(builder,
				fmt.Sprintf("persistent_keepalive_interval=%d\n", peer.PersistentKeepaliveSecond)...)
		}
		builder = append(builder, "replace_allowed_ips=true\n"...)
		for _, allowedIP := range peer.AllowedIPs {
			builder = append(builder, "allowed_ip="+allowedIP+"\n"...)
		}
	}
	return string(builder)
}

// start 在 127.0.0.1 上监听并启动 accept 循环。
func (fwd *forward) start(rt *runtime) error {
	listener, err := net.Listen("tcp", fwd.config.Listen)
	if err != nil {
		return err
	}
	fwd.listener = listener
	rt.wg.Add(1)
	go fwd.serve(rt)
	return nil
}

// serve 接受本地连接并交给 handleForwardConn 处理。
func (fwd *forward) serve(rt *runtime) {
	defer rt.wg.Done()
	for {
		localConn, err := fwd.listener.Accept()
		if err != nil {
			if rt.ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return
			}
			rt.log.warnf("转发 %s: accept 失败: %v", fwd.config.Listen, err)
			select {
			case <-rt.ctx.Done():
				return
			case <-time.After(time.Second):
			}
			continue
		}
		fwd.total.Add(1)
		rt.wg.Add(1)
		go rt.handleForwardConn(fwd, localConn)
	}
}

// handleForwardConn 把一条本地连接经隧道拨号到目标地址并双向拷贝。
// 拨号失败不退出进程：记录 last_error 并关闭该连接，等待下一次 accept。
func (rt *runtime) handleForwardConn(fwd *forward, localConn net.Conn) {
	defer rt.wg.Done()
	fwd.active.Add(1)
	defer fwd.active.Add(-1)
	rt.trackConn(localConn, true)
	defer rt.trackConn(localConn, false)
	defer localConn.Close()

	dialCtx, cancel := context.WithTimeout(rt.ctx, forwardDialTimeout)
	remoteConn, err := rt.tnet.DialContext(dialCtx, "tcp", fwd.config.Target)
	cancel()
	if err != nil {
		if changed := fwd.setLastError(err); changed {
			rt.log.warnf("转发 %s -> %s 拨号失败: %v", fwd.config.Listen, fwd.config.Target, err)
		}
		return
	}
	fwd.clearLastError()
	rt.trackConn(remoteConn, true)
	defer rt.trackConn(remoteConn, false)
	defer remoteConn.Close()
	rt.log.debugf("转发 %s -> %s 连接建立", fwd.config.Listen, fwd.config.Target)

	pipeBidirectional(localConn, remoteConn)
}

// pipeBidirectional 在两个连接之间双向拷贝，任一侧结束后关闭两侧。
func pipeBidirectional(left, right net.Conn) {
	var closeOnce sync.Once
	closeBoth := func() {
		closeOnce.Do(func() {
			left.Close()
			right.Close()
		})
	}
	var copyWait sync.WaitGroup
	copyWait.Add(1)
	go func() {
		defer copyWait.Done()
		_, _ = io.Copy(left, right)
		closeBoth()
	}()
	_, _ = io.Copy(right, left)
	closeBoth()
	copyWait.Wait()
}

func (rt *runtime) trackConn(conn net.Conn, add bool) {
	if add {
		rt.conns.Store(conn, struct{}{})
		if rt.ctx.Err() != nil {
			conn.Close()
		}
		return
	}
	rt.conns.Delete(conn)
}

func (rt *runtime) closeConnections() {
	rt.conns.Range(func(key, _ any) bool {
		if conn, ok := key.(net.Conn); ok {
			conn.Close()
		}
		return true
	})
}

// deviceState 读取 WireGuard 设备的运行时状态；失败时返回 nil（状态文件降级展示配置值）。
func (rt *runtime) deviceState() *ipcDeviceState {
	if rt.dev == nil {
		return nil
	}
	text, err := rt.dev.IpcGet()
	if err != nil {
		rt.log.debugf("读取 WireGuard 设备状态失败: %v", err)
		return nil
	}
	return parseIpcGet(text)
}

// snapshotStatus 汇总当前状态，用于写状态文件。
func (rt *runtime) snapshotStatus(now time.Time) *statusFile {
	status := &statusFile{
		Running:       true,
		Version:       version,
		UpdatedAtUnix: now.Unix(),
		AppPort:       rt.cfg.AppPort,
	}
	status.Interface = statusInterface{
		ListenPort: rt.cfg.Interface.ListenPort,
		Addresses:  append([]string{}, rt.cfg.Interface.Addresses...),
		PublicKey:  rt.publicKey,
	}
	state := rt.deviceState()
	if state != nil && state.listenPort != 0 {
		status.Interface.ListenPort = state.listenPort
	}

	status.Peers = make([]statusPeer, 0, len(rt.cfg.Peers))
	for i := range rt.cfg.Peers {
		peer := &rt.cfg.Peers[i]
		item := statusPeer{
			PublicKey:  peer.PublicKey,
			Endpoint:   peer.Endpoint,
			AllowedIPs: append([]string{}, peer.AllowedIPs...),
		}
		if state != nil {
			if runtimePeer, ok := state.peers[hexKey(peer.publicKey)]; ok {
				if runtimePeer.endpoint != "" {
					item.Endpoint = runtimePeer.endpoint
				}
				item.LastHandshakeUnix = runtimePeer.lastHandshakeSec
				item.RxBytes = runtimePeer.rxBytes
				item.TxBytes = runtimePeer.txBytes
			}
		}
		status.Peers = append(status.Peers, item)
	}

	status.Forwards = make([]statusForward, 0, len(rt.forwards))
	for _, fwd := range rt.forwards {
		status.Forwards = append(status.Forwards, statusForward{
			Listen:            fwd.config.Listen,
			Target:            fwd.config.Target,
			ActiveConnections: fwd.active.Load(),
			TotalConnections:  fwd.total.Load(),
			LastError:         fwd.snapshotLastError(),
		})
	}
	return status
}

// startStatusWriter 立即写一次状态文件，随后按周期刷新。
// 首次写入失败视为运行环境错误（否则插件无法判断 helper 健康状态）。
func (rt *runtime) startStatusWriter(path string, interval time.Duration) error {
	if path == "" {
		rt.log.infof("未指定 --status-file，不写状态文件")
		return nil
	}
	write := func() error {
		return writeStatusFile(path, rt.snapshotStatus(time.Now()))
	}
	if err := write(); err != nil {
		return fmt.Errorf("写入状态文件 %s 失败: %w", path, err)
	}
	rt.log.infof("状态文件: %s（每 %s 刷新）", path, interval)

	rt.wg.Add(1)
	go func() {
		defer rt.wg.Done()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		lastErr := ""
		for {
			select {
			case <-rt.ctx.Done():
				return
			case <-ticker.C:
				if err := write(); err != nil {
					message := err.Error()
					if message != lastErr {
						rt.log.warnf("写状态文件失败: %v", err)
						lastErr = message
					}
					continue
				}
				lastErr = ""
			}
		}
	}()
	return nil
}

// shutdown 取消上下文、关闭监听与活动连接，最后关闭 WireGuard 设备。可重复调用。
func (rt *runtime) shutdown() {
	rt.shutdownOnce.Do(func() {
		if rt.app != nil {
			rt.app.Close()
		}
		rt.cancel()
		if rt.socks != nil {
			rt.socks.Close()
		}
		for _, fwd := range rt.forwards {
			if fwd.listener != nil {
				fwd.listener.Close()
			}
		}
		rt.closeConnections()

		done := make(chan struct{})
		go func() {
			rt.wg.Wait()
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(shutdownTimeout):
			rt.log.warnf("关闭超时: 仍有连接处理协程未退出")
		}

		if rt.dev != nil {
			rt.dev.Close()
			rt.dev = nil
		}
	})
}

func (fwd *forward) setLastError(err error) bool {
	fwd.errMu.Lock()
	defer fwd.errMu.Unlock()
	message := err.Error()
	if message == fwd.lastErr {
		return false
	}
	fwd.lastErr = message
	return true
}

func (fwd *forward) clearLastError() {
	fwd.errMu.Lock()
	defer fwd.errMu.Unlock()
	fwd.lastErr = ""
}

func (fwd *forward) snapshotLastError() string {
	fwd.errMu.Lock()
	defer fwd.errMu.Unlock()
	return fwd.lastErr
}
