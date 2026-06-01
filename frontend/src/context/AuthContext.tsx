import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { ApiError, apiGet, apiPost, getSessionToken, setSessionToken } from '../api/client';

export interface AuthUser {
  user_id: string;
  email: string;
  is_admin: boolean;
  session_token?: string | null;
}

const USER_CACHE_KEY = 'plotwise_user_cache';

interface AuthConfig {
  auth_enabled: boolean;
  allow_register: boolean;
}

interface AuthContextValue {
  loading: boolean;
  authEnabled: boolean;
  allowRegister: boolean;
  user: AuthUser | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function loadCachedUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AuthUser;
    if (!parsed.user_id || !parsed.email) return null;
    return { user_id: parsed.user_id, email: parsed.email, is_admin: !!parsed.is_admin };
  } catch {
    return null;
  }
}

function cacheUser(user: AuthUser | null): void {
  try {
    if (user) {
      localStorage.setItem(USER_CACHE_KEY, JSON.stringify(user));
    } else {
      localStorage.removeItem(USER_CACHE_KEY);
    }
  } catch {
    /* ignore */
  }
}

function applySessionFromAuthResponse(me: AuthUser): AuthUser {
  if (me.session_token) {
    setSessionToken(me.session_token);
  }
  const user = { user_id: me.user_id, email: me.email, is_admin: me.is_admin };
  cacheUser(user);
  return user;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [allowRegister, setAllowRegister] = useState(true);
  const [user, setUser] = useState<AuthUser | null>(() =>
    getSessionToken() ? loadCachedUser() : null,
  );

  const refresh = useCallback(async () => {
    try {
      const config = await apiGet<AuthConfig>('/auth/config');
      setAuthEnabled(config.auth_enabled);
      setAllowRegister(config.allow_register);
      if (!config.auth_enabled) {
        setUser(null);
        setSessionToken(null);
        cacheUser(null);
        return;
      }

      const token = getSessionToken();
      if (!token) {
        setUser(null);
        cacheUser(null);
        return;
      }

      try {
        const me = await apiGet<AuthUser & { auth_enabled: boolean }>('/auth/me');
        const next = { user_id: me.user_id, email: me.email, is_admin: me.is_admin };
        setUser(next);
        cacheUser(next);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setSessionToken(null);
          cacheUser(null);
          setUser(null);
        } else {
          // Transient error — keep token; show cached profile until next refresh.
          const cached = loadCachedUser();
          setUser(cached);
        }
      }
    } catch {
      setAuthEnabled(false);
      setUser(null);
      setSessionToken(null);
      cacheUser(null);
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const login = useCallback(async (email: string, password: string) => {
    const me = await apiPost<AuthUser & { auth_enabled: boolean }>('/auth/login', { email, password });
    setUser(applySessionFromAuthResponse(me));
    setAuthEnabled(true);
  }, []);

  const register = useCallback(async (email: string, password: string) => {
    const me = await apiPost<AuthUser & { auth_enabled: boolean }>('/auth/register', { email, password });
    setUser(applySessionFromAuthResponse(me));
    setAuthEnabled(true);
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiPost('/auth/logout', {});
    } finally {
      setSessionToken(null);
      cacheUser(null);
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ loading, authEnabled, allowRegister, user, login, register, logout }),
    [loading, authEnabled, allowRegister, user, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
