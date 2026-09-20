import { GuidePage } from "../_guide";

export default function WindowsGuidePage() {
  return <GuidePage platform="Windows" importSteps={[
    "打开 Clash Verge / Mihomo 类客户端。",
    "找到订阅、Profile 或配置管理入口。",
    "新建远程订阅，粘贴你的订阅链接并保存。",
    "执行更新，选择刚导入的配置。",
    "开启系统代理；按客户端能力和需要选择是否开启 TUN。",
  ]} />;
}
