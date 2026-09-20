import { GuideLinks } from "./_guide";

export default function GuidesPage() {
  return <main>
    <p className="eyebrow">CLIENT GUIDES</p>
    <h1>客户端配置指南</h1>
    <p className="lead">选择你的设备平台，了解如何从 Lingsway 客户中心复制订阅链接，并在兼容客户端中导入和更新配置。</p>
    <GuideLinks />
    <p className="muted">订阅链接是访问凭据，请勿公开分享。</p>
  </main>;
}
