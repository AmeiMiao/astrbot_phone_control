package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/device"
)

// 日志级别：数值越大越详细。
const (
	levelSilent = iota
	levelError
	levelWarn
	levelInfo
	levelDebug
)

var levelNames = map[int]string{
	levelError: "ERROR",
	levelWarn:  "WARN",
	levelInfo:  "INFO",
	levelDebug: "DEBUG",
}

// logger 把日志逐行写入 stderr（契约要求日志走 stderr，且一行一条纯文本）。
type logger struct {
	level int
	mu    sync.Mutex
}

// newLogger 按配置的 log_level 创建 logger；未知取值按 info 处理。
func newLogger(level string) *logger {
	l := &logger{level: levelInfo}
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "silent", "off", "none":
		l.level = levelSilent
	case "error":
		l.level = levelError
	case "warn", "warning":
		l.level = levelWarn
	case "debug", "verbose":
		l.level = levelDebug
	}
	return l
}

func (l *logger) logf(minLevel int, format string, args ...any) {
	if l == nil || minLevel > l.level {
		return
	}
	msg := fmt.Sprintf(format, args...)
	msg = strings.ReplaceAll(msg, "\n", " ")
	l.mu.Lock()
	defer l.mu.Unlock()
	fmt.Fprintf(os.Stderr, "%s %s %s\n", time.Now().Format(time.RFC3339), levelNames[minLevel], msg)
}

func (l *logger) errorf(format string, args ...any) { l.logf(levelError, format, args...) }
func (l *logger) warnf(format string, args ...any)  { l.logf(levelWarn, format, args...) }
func (l *logger) infof(format string, args ...any)  { l.logf(levelInfo, format, args...) }
func (l *logger) debugf(format string, args ...any) { l.logf(levelDebug, format, args...) }

// deviceLogger 返回 wireguard-go 的日志适配器。
// wireguard-go 自带的 device.NewLogger 会写到 stdout，这里统一改到 stderr。
func (l *logger) deviceLogger() *device.Logger {
	return &device.Logger{
		Verbosef: l.debugf,
		Errorf:   l.errorf,
	}
}
