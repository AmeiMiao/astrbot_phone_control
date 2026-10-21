package main

import (
	"context"
	"io"
	"net"
	"testing"
	"time"
)

func TestSocksListenPolicy(t *testing.T) {
	for _, address := range []string{"0.0.0.0:1080", "[::1]:1080", "127.0.0.1:0", "127.0.0.1:15555"} {
		raw := validConfigJSON()
		raw["socks_listen"] = address
		if _, err := loadConfig(writeConfigFile(t, raw)); err == nil {
			t.Fatalf("接受非法 SOCKS 监听 %s", address)
		}
	}
	raw := validConfigJSON()
	raw["socks_listen"] = "127.0.0.1:1080"
	if _, err := loadConfig(writeConfigFile(t, raw)); err != nil {
		t.Fatal(err)
	}
}

func TestSocksRejectsUnsupportedAndOutsidePeer(t *testing.T) {
	cases := []struct {
		request []byte
		code    byte
	}{
		{[]byte{5, 3, 0, 1}, 7},
		{[]byte{5, 1, 0, 3}, 8},
		{[]byte{5, 1, 0, 1, 192, 0, 2, 1, 0x15, 0xb3}, 2},
	}
	for _, test := range cases {
		cfg, err := loadConfig(writeConfigFile(t, validConfigJSON()))
		if err != nil {
			t.Fatal(err)
		}
		rt := &runtime{cfg: cfg, ctx: context.Background()}
		client, server := net.Pipe()
		client.SetDeadline(time.Now().Add(time.Second))
		rt.wg.Add(1)
		go rt.handleSocks(server)
		if _, err := client.Write([]byte{5, 1, 0}); err != nil {
			t.Fatal(err)
		}
		var method [2]byte
		if _, err := io.ReadFull(client, method[:]); err != nil || method != [2]byte{5, 0} {
			t.Fatalf("协商失败 %v %v", method, err)
		}
		if _, err := client.Write(test.request); err != nil {
			t.Fatal(err)
		}
		var reply [10]byte
		if _, err := io.ReadFull(client, reply[:]); err != nil || reply[1] != test.code {
			t.Fatalf("拒绝码 %v，期望 %v，错误 %v", reply, test.code, err)
		}
		client.Close()
		rt.wg.Wait()
	}
}
