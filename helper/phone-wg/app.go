package main

import (
	"context"
	"net"
	"strconv"
)

// serveApp只接收配置手机的虚拟网连接，将固定证书TLS字节流送往本机App入口。
func (rt *runtime) serveApp() {
	defer rt.wg.Done()
	for {
		incoming, err := rt.app.Accept()
		if err != nil {
			return
		}
		host, _, err := net.SplitHostPort(incoming.RemoteAddr().String())
		if err != nil || host != rt.cfg.AppPeer {
			incoming.Close()
			continue
		}
		select {
		case rt.appSlots <- struct{}{}:
		default:
			incoming.Close()
			continue
		}
		rt.trackConn(incoming, true)
		rt.wg.Add(1)
		go func() {
			defer rt.wg.Done()
			defer func() { <-rt.appSlots }()
			defer rt.trackConn(incoming, false)
			defer incoming.Close()
			ctx, cancel := context.WithTimeout(rt.ctx, forwardDialTimeout)
			defer cancel()
			dialer := net.Dialer{}
			local, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(rt.cfg.AppPort)))
			if err != nil {
				return
			}
			rt.trackConn(local, true)
			defer rt.trackConn(local, false)
			defer local.Close()
			pipeBidirectional(incoming, local)
		}()
	}
}
