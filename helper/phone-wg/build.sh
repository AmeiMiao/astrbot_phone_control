#!/usr/bin/env bash
# 构建 phone-wg：linux/amd64 静态二进制（不启用 CGO）
#
# 默认输出到本目录 bin/phone-wg，可复制到插件 bin/linux-x86_64/ 随插件分发。
# 本机没有 Go 时自动改用 docker 镜像构建（需 docker 权限）：
#   BUILD_IMAGE=golang:1.26-alpine ./build.sh
set -euo pipefail

cd "$(dirname "$0")"

GO_VERSION="${GO_VERSION:-1.26}"
BUILD_IMAGE="${BUILD_IMAGE:-golang:${GO_VERSION}-alpine}"
OUT_DIR="${OUT_DIR:-bin}"
OUT_FILE="${OUT_FILE:-${OUT_DIR}/phone-wg}"

export CGO_ENABLED=0
export GOOS=linux
export GOARCH=amd64
export GOFLAGS="${GOFLAGS:--mod=mod}"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export GOSUMDB="${GOSUMDB:-off}"

run_build() {
	go mod tidy
	go vet ./...
	go test ./...
	mkdir -p "$OUT_DIR"
	go build -trimpath -ldflags "-s -w" -o "$OUT_FILE" .
}

if command -v go >/dev/null 2>&1; then
	echo "==> 使用本机 go: $(go version)"
	run_build
else
	echo "==> 本机未找到 go，使用 docker 镜像 ${BUILD_IMAGE}"
	mkdir -p "$OUT_DIR"
	docker run --rm -v "${PWD}:/src" -w /src \
		-e CGO_ENABLED=0 -e GOOS=linux -e GOARCH=amd64 \
		-e "GOFLAGS=${GOFLAGS}" -e "GOPROXY=${GOPROXY}" -e "GOSUMDB=${GOSUMDB}" \
		"${BUILD_IMAGE}" sh -c "go mod tidy && go vet ./... && go test ./... && go build -trimpath -ldflags '-s -w' -o '${OUT_FILE}' ."
fi

echo "==> 构建完成: ${OUT_FILE}"
ls -l "$OUT_FILE"
if command -v file >/dev/null 2>&1; then
	file "$OUT_FILE"
fi
if command -v sha256sum >/dev/null 2>&1; then
	sha256sum "$OUT_FILE"
fi
