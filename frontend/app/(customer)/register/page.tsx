"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, AuthCard, saveCustomerToken } from "../../customer";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [nickname, setNickname] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(""); setMessage("");
    const response = await fetch(`${api}/auth/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, nickname: nickname || null, password }) });
    if (!response.ok) { const body = await response.json().catch(() => ({})); setError(body.detail ?? "注册失败"); return; }
    const login = await fetch(`${api}/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
    if (!login.ok) { setMessage("注册成功，请登录"); return; }
    const body = await login.json() as { access_token: string }; saveCustomerToken(body.access_token); router.push("/portal");
  }
  return <AuthCard><h1>注册</h1><p className="muted">先完成基础注册，邮箱验证稍后接入。</p><form className="panel form-panel" onSubmit={submit}><label>邮箱<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label><label>昵称（可选）<input value={nickname} onChange={(e) => setNickname(e.target.value)} /></label><label>密码（至少 10 位）<input type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required /></label><button type="submit">注册并登录</button>{error && <p className="notice error">{error}</p>}{message && <p className="notice">{message}</p>}</form><p>已有账号？<Link href="/login">登录</Link></p></AuthCard>;
}
