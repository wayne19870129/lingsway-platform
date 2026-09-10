"use client";

import Link from "next/link";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";

import { api } from "../lib/api";
const CUSTOMER_TOKEN_KEY = "lingsway_customer_access_token";

type CustomerContextValue = {
  token: string;
  request: (path: string, init?: RequestInit) => Promise<Response>;
  logout: () => void;
};
const CustomerContext = createContext<CustomerContextValue | null>(null);

export function useCustomer(): CustomerContextValue {
  const value = useContext(CustomerContext);
  if (!value) throw new Error("useCustomer must be used inside CustomerShell");
  return value;
}

export function saveCustomerToken(token: string): void {
  window.localStorage.setItem(CUSTOMER_TOKEN_KEY, token);
}

type ValidationDetail = { msg?: string }[];

export function extractErrorMessage(body: { detail?: string | ValidationDetail }, fallback: string): string {
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join("; ") || fallback;
  return fallback;
}

export function CustomerShell({ title, children }: { title: string; children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 旧实现原样迁入，重构数据加载逻辑超出 T5h 迁移范围，另行处理
    setToken(window.localStorage.getItem(CUSTOMER_TOKEN_KEY));
    setReady(true);
  }, []);

  const logout = useCallback(() => {
    window.localStorage.removeItem(CUSTOMER_TOKEN_KEY);
    setToken(null);
  }, []);
  const request = useCallback(async (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token ?? ""}`);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${api}${path}`, { ...init, headers, cache: "no-store" });
    if (response.status === 401) logout();
    return response;
  }, [logout, token]);

  if (!ready) return <main><p className="notice">正在加载…</p></main>;
  if (!token) return <main><p className="eyebrow">CUSTOMER PORTAL</p><h1>{title}</h1><p className="lead">请先登录后继续。</p><div className="actions"><Link className="button-link" href="/login">登录</Link><Link className="button-link secondary" href="/register">注册</Link></div></main>;

  return <CustomerContext.Provider value={{ token, request, logout }}>
    <main className="customer-layout">
      <aside className="customer-sidebar">
        <Link className="customer-brand" href="/portal">LINGSWAY<span>RESIDENTIAL EGRESS</span></Link>
        <nav className="customer-side-nav" aria-label="客户导航">
          <div className="customer-nav-group"><span>订阅</span><Link href="/portal">仪表盘</Link><Link href="/plans">套餐</Link><Link href="/subscriptions">我的订阅</Link></div>
          <div className="customer-nav-group"><span>财务</span><Link href="/orders/new">新建订单</Link><Link href="/orders">订单列表</Link></div>
          <div className="customer-nav-group"><span>用户</span><Link href="/portal">账户信息</Link></div>
        </nav>
        <button className="button-secondary sidebar-logout" onClick={logout}>退出登录</button>
      </aside>
      <section className="customer-content"><div className="customer-heading"><p className="eyebrow">CUSTOMER PORTAL</p><h1>{title}</h1></div>{children}</section>
    </main>
  </CustomerContext.Provider>;
}

export function AuthCard({ children }: { children: ReactNode }) {
  return <main><p className="eyebrow">LINGSWAY ACCOUNT</p><div className="auth-card">{children}</div></main>;
}

export { api, CUSTOMER_TOKEN_KEY };
