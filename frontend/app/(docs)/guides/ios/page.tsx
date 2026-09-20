import { GuidePage } from "../_guide";

export default function IosGuidePage() {
  return <GuidePage platform="iOS" importSteps={[
    "安装支持远程 Clash/Mihomo subscription 的兼容客户端。",
    "在客户端找到“从 URL 导入”或“订阅”入口。",
    "粘贴你的订阅链接并保存。",
    "更新订阅，选择导入的配置。",
    "启动代理或 VPN profile。",
  ]} />;
}
