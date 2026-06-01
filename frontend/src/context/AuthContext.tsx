import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, setSessionToken } from '../api/client';

export interface AuthUser {
  user_id: string;
  email: string;
  is_admin: boolean;
  session_token?: string | null;
}

function applySessionFromAuthResponse(me: AuthUser): AuthUser {
  if (me.session_token) {
    setSessionToken(me.session_token);
  }
  return { user_id: me.user_id, email: me.email, is_admin: me.is_admin };
}

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

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [allowRegister, setAllowRegister] = useState(true);
  const [user, setUser] = useState<AuthUser | null>(null);

  const refresh = useCallback(async () => {
    try {
      const config = await apiGet<AuthConfig>('/auth/config');
      setAuthEnabled(config.auth_enabled);
      setAllowRegister(config.allow_register);
      if (!config.auth_enabled) {
        setUser(null);
        setSessionToken(null);
        return;
      }
      try {
        const me = await apiGet<AuthUser & { auth_enabled: boolean }>('/auth/me');
        setUser({ user_id: me.user_id, email: me.email, is_admin: me.is_admin });
      } catch {
        setUser(null);
        setSessionToken(null);
      }
    } catch {
      setAuthEnabled(false);
      setUser(null);
      setSessionToken(null);
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
    await apiPost('/auth/logout', {});
    setSessionToken(null);
    setUser(null);
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
