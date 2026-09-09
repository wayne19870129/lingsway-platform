import type { Metadata } from "next";
import Link from "next/link";
import "./styles.css";

export const metadata: Metadata = { title: "订阅服务平台", description: "稳定、可替换、可维护的订阅服务" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>
    <header className="site-header">
      <Link className="brand" href="/">订阅服务平台</Link>
      <nav aria-label="主导航">
        <Link href="/portal">用户登录</Link>
        <Link href="/admin">管理后台</Link>
      </nav>
    </header>
    {children}
  </body></html>;
}
