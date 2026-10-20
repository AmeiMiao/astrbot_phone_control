// phone-wg 是插件自带的 WireGuard userspace helper。
//
// 它基于 wireguard-go + tun/netstack 在用户态实现 WireGuard 设备与 TCP 转发，
// 不依赖 NET_ADMIN/NET_RAW、/dev/net/tun、内核 wireguard 模块或 ip/wg 工具，
// 因此可以在无特权容器里作为 WG client 或 WG server 运行。
//
// 二进制与插件 Python 侧通过命令行、配置 JSON、状态文件 JSON 交互；
// 接口约定见同目录 README.md。
package main

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"golang.org/x/crypto/curve25519"
)

// version 是 helper 版本号，同时写入状态文件的 version 字段。
const version = "0.1.0"

// 退出码约定（与插件侧对齐）：
//   0  正常退出（收到 SIGTERM/SIGINT 后优雅关闭）
//   2  命令行或配置错误
//   3  运行环境初始化失败（WG 设备、UDP 监听、TCP 转发监听等）
const (
	exitConfigError  = 2
	exitRuntimeError = 3
)

const usageText = `phone-wg - WireGuard userspace helper

用法:
  phone-wg run --config <配置文件> [--status-file <状态文件>]
  phone-wg genkey
  phone-wg pubkey --private-key <base64>
  phone-wg version

子命令:
  run      前台运行 WireGuard 设备与 TCP 转发；SIGTERM/SIGINT 优雅退出
  genkey   生成 32 字节 Curve25519 私钥并以 WireGuard 标准 base64 输出
  pubkey   由私钥推导公钥并以 base64 输出
  version  输出 helper 版本号
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usageText)
		os.Exit(exitConfigError)
	}

	switch os.Args[1] {
	case "run":
		os.Exit(runCommand(os.Args[2:]))
	case "genkey":
		os.Exit(genKeyCommand())
	case "pubkey":
		os.Exit(pubKeyCommand(os.Args[2:]))
	case "version", "--version", "-v":
		fmt.Println(version)
	case "help", "-h", "--help":
		fmt.Print(usageText)
	default:
		fmt.Fprintf(os.Stderr, "phone-wg: 未知子命令 %q\n\n%s", os.Args[1], usageText)
		os.Exit(exitConfigError)
	}
}

// runCommand 解析参数、加载配置并运行，直到收到退出信号。
func runCommand(args []string) int {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.Usage = func() { fmt.Fprint(os.Stderr, usageText) }
	configPath := fs.String("config", "", "配置 JSON 文件路径（必填）")
	statusPath := fs.String("status-file", "", "状态 JSON 文件路径（可选）")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return exitConfigError
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(os.Stderr, "phone-wg run: 无法识别的参数 %q\n", fs.Arg(0))
		return exitConfigError
	}
	if *configPath == "" {
		fmt.Fprintln(os.Stderr, "phone-wg run: 缺少 --config 参数")
		return exitConfigError
	}

	cfg, err := loadConfig(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "phone-wg run: 配置错误: %v\n", err)
		return exitConfigError
	}

	log := newLogger(cfg.LogLevel)
	log.infof("phone-wg %s 启动: config=%s addresses=%s listen_port=%d peers=%d forwards=%d",
		version, *configPath, strings.Join(cfg.Interface.Addresses, ","),
		cfg.Interface.ListenPort, len(cfg.Peers), len(cfg.Forwards))

	rt, err := startRuntime(cfg, log)
	if err != nil {
		log.errorf("初始化失败: %v", err)
		return exitRuntimeError
	}
	defer rt.shutdown()

	if err := rt.startStatusWriter(*statusPath, statusWriteInterval); err != nil {
		log.errorf("状态文件初始化失败: %v", err)
		return exitRuntimeError
	}

	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	sig := <-sigCh
	signal.Stop(sigCh)
	log.infof("收到信号 %s，开始优雅关闭", sig)

	rt.shutdown()
	log.infof("已停止")
	return 0
}

// genKeyCommand 生成 WireGuard 私钥（与 wg genkey 一致：随机 32 字节并 clamp）。
func genKeyCommand() int {
	var key [32]byte
	if _, err := rand.Read(key[:]); err != nil {
		fmt.Fprintf(os.Stderr, "phone-wg genkey: 生成随机数失败: %v\n", err)
		return exitRuntimeError
	}
	// Curve25519 标量 clamp，使输出与 WireGuard 工具链保持一致。
	key[0] &= 248
	key[31] &= 127
	key[31] |= 64
	fmt.Println(base64.StdEncoding.EncodeToString(key[:]))
	return 0
}

// pubKeyCommand 由私钥推导公钥。
func pubKeyCommand(args []string) int {
	fs := flag.NewFlagSet("pubkey", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.Usage = func() { fmt.Fprint(os.Stderr, usageText) }
	privateKey := fs.String("private-key", "", "base64 编码的 32 字节私钥（必填）")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return exitConfigError
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(os.Stderr, "phone-wg pubkey: 无法识别的参数 %q\n", fs.Arg(0))
		return exitConfigError
	}
	if *privateKey == "" {
		fmt.Fprintln(os.Stderr, "phone-wg pubkey: 缺少 --private-key 参数")
		return exitConfigError
	}

	priv, err := parseKeyBase64(*privateKey)
	if err != nil {
		fmt.Fprintf(os.Stderr, "phone-wg pubkey: 私钥无效: %v\n", err)
		return exitConfigError
	}
	pub, err := curve25519.X25519(priv[:], curve25519.Basepoint)
	if err != nil {
		fmt.Fprintf(os.Stderr, "phone-wg pubkey: 推导公钥失败: %v\n", err)
		return exitConfigError
	}
	fmt.Println(base64.StdEncoding.EncodeToString(pub))
	return 0
}
