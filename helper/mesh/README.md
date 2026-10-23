# 用户态组网组件

Tailscale CLI/tailscaled 使用官方固定版本 1.104.2 Linux amd64 归档，来源、SHA256 和许可证位于 `bin/linux-x86_64/NOTICE.txt` 与 `TAILSCALE-LICENSE.txt`。

ZeroTier 使用官方 ZeroTier Sockets SDK 固定提交 `a707ea6ae0910efdc1125d04758c411e2e9ea4f9`，在独立工作进程运行节点、回环 SOCKS 和来自指定手机 IP 的组网代理转发。随包 `libzt.so` 面向 Linux x86_64、glibc ≥ 2.38；C++ 运行库静态链接。其他平台可配置兼容 SDK 动态库路径。

构建需要 Git、CMake、C/C++ 编译器、Make 与 strip：

```bash
MESH_BUILD_DIR=/path/to/cache/mesh MESH_OUTPUT=/path/to/output ./build-zerotier.sh
```

编译选项 `-include stdexcept` 为上游指标库声明 `std::invalid_argument` 所需标准头。Central API 编译关闭；插件通过 Network ID 与本地节点身份加入网络，成员授权由 ZeroTier 控制台完成。保留原始 SDK/ZeroTierOne/lwIP 许可证，位于 `bin/linux-x86_64/ZEROTIER-LICENSE.txt` 和 `zerotier-licenses/`。
