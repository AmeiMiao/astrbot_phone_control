# 用户态组网组件

Tailscale CLI/tailscaled 在启用并重载时按 Linux 宿主架构从 https://pkgs.tailscale.com/stable/ 下载官方稳定版。归档与 SHA256 校验后保存到插件数据目录，manifest.json 记录实际版本与来源；手动路径优先。

ZeroTier 使用官方 ZeroTier Sockets SDK 固定提交 `a707ea6ae0910efdc1125d04758c411e2e9ea4f9`，在独立工作进程运行节点、回环 SOCKS 和来自指定手机 IP 的组网代理转发。`libzt` 仅支持手动提供，在配置中填写动态库绝对路径。请在目标宿主系统构建，以匹配架构和运行库；以下脚本静态链接 C++ 运行库。

构建需要 Git、CMake、C/C++ 编译器、Make 与 strip：

```bash
MESH_BUILD_DIR=/path/to/cache/mesh MESH_OUTPUT=/path/to/output ./build-zerotier.sh
```

编译选项 `-include stdexcept` 为上游指标库声明 `std::invalid_argument` 所需标准头。Central API 编译关闭；插件通过 Network ID 与本地节点身份加入网络，成员授权由 ZeroTier 控制台完成。保留原始 SDK/ZeroTierOne/lwIP 许可证，位于 `bin/linux-x86_64/ZEROTIER-LICENSE.txt` 和 `zerotier-licenses/`。

SDK 的虚拟接口 MTU 为 1280，TCP MSS 随之计算，使封装后的流量适合常见 1500 MTU 链路。构建脚本应用同目录 `zerotier-mtu.patch`；每个 SDK socket 的收发与关闭由同一线程处理，代理保留 TCP 半关闭并使用有界缓冲。

ZeroTier 的强制 TCP 中继由 `zts_init_force_tcp_relay` 在节点启动前设置，默认按 SDK 自动策略运行。开启时要求动态库提供此接口；能力不足会明确报错。所选传输策略写入节点状态，配置变更重建工作进程并复用节点身份。
