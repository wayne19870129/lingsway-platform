import Link from "next/link";
import type { ReactNode } from "react";

type GuideProps = {
  platform: string;
  importSteps: string[];
};

function Steps({ items }: { items: string[] }) {
  return <ol>{items.map((item) => <li key={item}>{item}</li>)}</ol>;
}

export function GuidePage({ platform, importSteps }: GuideProps) {
  return (
    <main>
      <p className="eyebrow">CLIENT GUIDE</p>
      <h1>{platform} 客户端使用指南</h1>
      <p className="lead">拿到订阅链接后，按照下面的步骤在 {platform} 上导入并更新配置。</p>

      <section className="panel">
        <h2>开始前准备</h2>
        <Steps items={[
          "登录 Lingsway 客户中心。",
          "打开“我的订阅”，进入对应的订阅详情。",
          "找到“订阅链接”，点击“复制链接”。",
        ]} />
        <p className="notice">订阅链接相当于访问凭据，请勿发送给其他人或公开发布。</p>
      </section>

      <section className="panel">
        <h2>导入订阅</h2>
        <p>当前推荐使用 Clash Verge / Mihomo 内核兼容的订阅方式。客户端的入口名称可能因版本不同而有所差异。</p>
        <Steps items={importSteps} />
      </section>

      <section className="panel">
        <h2>更新订阅</h2>
        <p>服务端节点或配置变化后，在客户端打开订阅/Profile，执行“更新订阅”，等待刷新完成，再选择需要使用的节点或策略。</p>
        <p><strong>更新订阅</strong>是继续使用原链接拉取新配置；<strong>刷新订阅链接</strong>是在客户中心生成新的访问链接，旧链接会立即失效。</p>
      </section>

      <section className="panel">
        <h2>刷新订阅链接</h2>
        <p>如果怀疑订阅链接泄漏或被他人使用：</p>
        <Steps items={[
          "进入客户中心的订阅详情，使用“刷新订阅链接”。",
          "旧链接立即失效；复制新生成的链接。",
          "删除客户端中的旧订阅。",
          "使用新链接重新导入并更新。",
        ]} />
        <p className="notice">刷新后，旧客户端不会继续自动使用旧链接；必须重新导入新链接。</p>
      </section>

      <section className="panel">
        <h2>常见问题</h2>
        <h3>订阅添加后没有节点</h3>
        <p>手动执行一次更新，确认使用的是最新订阅链接。若刚刷新过链接，请删除旧订阅后重新导入。不要直接修改订阅 URL。</p>
        <h3>客户端显示更新失败</h3>
        <p>检查网络和链接是否完整，并确认是否刚在客户中心刷新过订阅链接；必要时重新复制最新链接。不要关闭 TLS 或证书安全验证。</p>
        <h3>更换设备</h3>
        <p>可以在新设备中重新导入自己的订阅链接，但不要公开分享订阅凭据。</p>
        <h3>可以直接修改生成的配置吗？</h3>
        <p>服务端订阅是权威来源。客户端本地修改可能在下一次订阅更新时被覆盖，不应视为平台支持的持久配置方式。</p>
      </section>

      <div className="actions">
        <Link className="button-link secondary" href="/guides">返回指南首页</Link>
        <Link className="button-link" href="/login">进入客户中心</Link>
      </div>
    </main>
  );
}

export function GuideLinks(): ReactNode {
  return <div className="layers">
    <Link className="layer" href="/guides/windows"><span>WINDOWS</span>Windows 指南</Link>
    <Link className="layer" href="/guides/macos"><span>MACOS</span>macOS 指南</Link>
    <Link className="layer" href="/guides/android"><span>ANDROID</span>Android 指南</Link>
    <Link className="layer" href="/guides/ios"><span>IOS</span>iOS 指南</Link>
  </div>;
}
