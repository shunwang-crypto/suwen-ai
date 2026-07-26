"use client";

// AuthProvider：access token 内存态 + refresh cookie 静默续期。
// 挂载时先 refresh 再 /api/auth/me；401 全局回调清态（AppShell 负责跳登录）。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  apiFetch,
  apiJson,
  refreshAccessToken,
  setAccessToken,
  setOnUnauthorized,
} from "./api";
import type { TokenResponse, UserOut } from "./types";

export type AuthStatus = "loading" | "authed" | "guest";

interface AuthContextValue {
  user: UserOut | null;
  status: AuthStatus;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserOut | null>(null);
  const [status, setStatus] = useState<AuthStatus>("loading");

  const clearAuth = useCallback(() => {
    setAccessToken(null);
    setUser(null);
    setStatus("guest");
  }, []);

  // 全局 401 回调：任何请求刷新失败后走这里
  useEffect(() => {
    setOnUnauthorized(clearAuth);
    return () => setOnUnauthorized(null);
  }, [clearAuth]);

  // 页面刷新后的静默续期
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const refreshed = await refreshAccessToken();
      if (cancelled) return;
      if (!refreshed) {
        clearAuth();
        return;
      }
      try {
        const me = await apiJson<UserOut>("/api/auth/me");
        if (cancelled) return;
        setUser(me);
        setStatus("authed");
      } catch {
        if (!cancelled) clearAuth();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clearAuth]);

  const login = useCallback(async (email: string, password: string) => {
    const token = await apiJson<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: { email, password },
    });
    setAccessToken(token.access_token);
    const me = await apiJson<UserOut>("/api/auth/me");
    setUser(me);
    setStatus("authed");
  }, []);

  const register = useCallback(
    async (email: string, password: string) => {
      await apiJson<UserOut>("/api/auth/register", {
        method: "POST",
        body: { email, password },
      });
      // 注册成功后自动登录
      await login(email, password);
    },
    [login],
  );

  const logout = useCallback(async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch {
      // 即使服务端登出失败也清本地态
    } finally {
      clearAuth();
    }
  }, [clearAuth]);

  const refreshMe = useCallback(async () => {
    const me = await apiJson<UserOut>("/api/auth/me");
    setUser(me);
    setStatus("authed");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, status, login, register, logout, refreshMe }),
    [user, status, login, register, logout, refreshMe],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 <AuthProvider> 内使用");
  return ctx;
}
