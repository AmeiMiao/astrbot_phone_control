package main

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"strconv"
	"time"
)

// serveSocks 提供环回 SOCKS5 CONNECT，目的地址受 peer AllowedIPs 约束。
func (rt *runtime) serveSocks() {
	defer rt.wg.Done()
	for {
		conn, err := rt.socks.Accept()
		if err != nil {
			return
		}
		rt.wg.Add(1)
		go rt.handleSocks(conn)
	}
}

func (rt *runtime) handleSocks(local net.Conn) {
	defer rt.wg.Done()
	rt.trackConn(local, true)
	defer rt.trackConn(local, false)
	defer local.Close()
	local.SetDeadline(time.Now().Add(10 * time.Second))
	var header [2]byte
	if _, err := io.ReadFull(local, header[:]); err != nil || header[0] != 5 || header[1] == 0 {
		return
	}
	methods := make([]byte, int(header[1]))
	if _, err := io.ReadFull(local, methods); err != nil {
		return
	}
	allowed := false
	for _, method := range methods {
		allowed = allowed || method == 0
	}
	if !allowed {
		local.Write([]byte{5, 255})
		return
	}
	if _, err := local.Write([]byte{5, 0}); err != nil {
		return
	}
	var request [4]byte
	if _, err := io.ReadFull(local, request[:]); err != nil {
		return
	}
	reply := func(code byte) { local.Write([]byte{5, code, 0, 1, 0, 0, 0, 0, 0, 0}) }
	if request[0] != 5 || request[2] != 0 {
		reply(1)
		return
	}
	if request[1] != 1 {
		reply(7)
		return
	}
	size := 4
	if request[3] == 4 {
		size = 16
	} else if request[3] != 1 {
		reply(8)
		return
	}
	raw := make([]byte, size+2)
	if _, err := io.ReadFull(local, raw); err != nil {
		return
	}
	address, ok := netip.AddrFromSlice(raw[:size])
	port := binary.BigEndian.Uint16(raw[size:])
	if !ok || port == 0 || !rt.cfg.targetCoveredByPeers(address.Unmap()) {
		reply(2)
		return
	}
	ctx, cancel := context.WithTimeout(rt.ctx, forwardDialTimeout)
	remote, err := rt.tnet.DialContext(ctx, "tcp", net.JoinHostPort(address.String(), strconv.Itoa(int(port))))
	cancel()
	if err != nil {
		reply(5)
		return
	}
	rt.trackConn(remote, true)
	defer rt.trackConn(remote, false)
	defer remote.Close()
	reply(0)
	local.SetDeadline(time.Time{})
	pipeBidirectional(local, remote)
}
